# 02 — Data Engine

> **Niveau** : Senior Engineers, Quant Researchers  
> **Fichiers** : `market_data/service.py`, `infrastructure/repositories/db_repositories.py`, `migrations/init.sql`

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [DataNormalizer](#2-datanormalizer)
3. [MT5DataProvider](#3-mt5dataprovider)
4. [BinanceDataProvider](#4-binancedataprovider)
5. [HistoricalDataManager](#5-historicaldatamanager)
6. [GBM Synthetic Fallback](#6-gbm-synthetic-fallback)
7. [Persistance TimescaleDB](#7-persistance-timescaledb)
8. [Schéma de base de données](#8-schéma-de-base-de-données)
9. [Routing par classe d'actif](#9-routing-par-classe-dactif)
10. [Considérations de production](#10-considérations-de-production)

---

## 1. Vue d'ensemble

Le Data Engine est la **couche d'ingestion**. Son unique responsabilité est de transformer des données brutes hétérogènes (MT5, Binance, CSV, synthétiques) en événements de domaine canoniques (`TickEvent`, `BarEvent`).

```
 MetaTrader 5          Binance WebSocket       CSV / Synthetic
 (C++ DLL sync)        (aiohttp async)         (pandas)
      │                      │                      │
      ▼                      ▼                      ▼
 MT5DataProvider      BinanceDataProvider    HistoricalDataManager
      │                      │                      │
      └──────────────────────┴──────────────────────┘
                             │
                       DataNormalizer
                             │
                   ┌─────────┴──────────┐
                   ▼                    ▼
               TickEvent            BarEvent
                   │                    │
            stream:ticks          stream:bars
              (Redis)               (Redis)
```

**Principe fondamental** : à partir de la `DataNormalizer`, tout le code aval est **indépendant du broker**. Remplacer MT5 par Interactive Brokers nécessite uniquement un nouveau provider.

---

## 2. DataNormalizer

Classe de conversion stateless. Toutes les méthodes sont `@staticmethod` — aucun état partagé, facilement testable.

### Conversions effectuées

| Source | Champ brut | Champ normalisé | Transformation |
|---|---|---|---|
| MT5 | `time` (Unix int) | `timestamp` (UTC datetime) | `fromtimestamp(..., tz=UTC)` |
| MT5 | `tick_volume` | `volume` | Renommage |
| Binance | `T` (ms) | `timestamp` | Division par 1000 |
| Binance | `p` / `q` | `bid=ask=price`, `volume` | Trade stream → pseudo-tick |
| Synthétique | raw float | `BarEvent` | GBM simulation |

### Méthodes principales

```python
DataNormalizer.mt5_tick_to_event(raw: dict) -> TickEvent
DataNormalizer.mt5_bar_to_event(raw: dict, symbol, timeframe, idx) -> BarEvent
DataNormalizer.ohlcv_to_dataframe(bars: List[BarEvent]) -> pd.DataFrame
DataNormalizer.binance_ws_trade_to_event(msg: dict) -> TickEvent
```

### Exemple : tick MT5 → TickEvent

```python
# Donnée brute de MetaTrader 5
raw_tick = {
    "symbol": "EURUSD",
    "bid": 1.08512,
    "ask": 1.08514,
    "volume_real": 2.3,
    "time": 1706789412,
}

# Normalisation
event = DataNormalizer.mt5_tick_to_event(raw_tick)
# → TickEvent(
#       symbol="EURUSD",
#       bid=1.08512,
#       ask=1.08514,
#       volume=2.3,
#       timestamp=datetime(2024, 2, 1, 10, 30, 12, tzinfo=UTC),
#       mid=1.08513,
#       spread=0.00002,
#   )
```

---

## 3. MT5DataProvider

### Problème architectural

L'API Python MetaTrader 5 est un wrapper de DLL C++. Toutes ses fonctions sont **synchrones et bloquantes**. Appeler `mt5.copy_rates_range()` directement depuis une coroutine asyncio bloquerait l'ensemble de l'event loop pendant la durée du call réseau (20-500 ms).

### Solution : ThreadPoolExecutor isolé

```python
class MT5DataProvider:
    def __init__(self):
        # Pool de 4 threads — correspond à la limite interne MT5
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="mt5"
        )

    async def get_historical_bars(self, symbol, timeframe, start, end, count):
        loop = asyncio.get_event_loop()
        # Exécution synchrone dans un thread isolé
        # → l'event loop asyncio reste libre
        bars = await loop.run_in_executor(
            self._executor,
            self._fetch_bars,     # méthode synchrone
            symbol, timeframe, start, end, count,
        )
        return bars
```

### Polling des ticks

MT5 n'a pas de callback natif pour les ticks. La solution est un **polling asynchrone à 100 ms** :

```python
async def _poll_ticks(self) -> None:
    while self._connected:
        for symbol, handlers in self._subscriptions.items():
            raw = await loop.run_in_executor(
                self._executor,
                self._get_last_tick,
                symbol,
            )
            if raw:
                event = DataNormalizer.mt5_tick_to_event(raw)
                for handler in handlers:
                    await handler(event)
        await asyncio.sleep(0.1)   # 100 ms → 10 ticks/sec max
```

> **Note production** : Pour du HFT, utiliser `mt5.copy_ticks_from()` dans un thread dédié avec un `asyncio.Queue` pour bridger vers le loop principal.

### Reconnexion automatique

```python
async def connect(self) -> bool:
    for attempt in range(5):
        success = await loop.run_in_executor(
            self._executor, self._mt5_connect
        )
        if success:
            return True
        backoff = 2 ** attempt
        await asyncio.sleep(backoff)
    return False
```

### Fallback simulation

Si MT5 n'est pas installé (CI, développement), le provider génère des données synthétiques GBM :

```python
try:
    import MetaTrader5 as mt5
    # ... appel réel
except ImportError:
    logger.warning("MT5 non installé — mode simulation activé")
    return self._generate_synthetic_bars(symbol, timeframe, count)
```

---

## 4. BinanceDataProvider

Provider natif async — pas de ThreadPoolExecutor nécessaire.

### Architecture WebSocket

```
wss://stream.binance.com:9443/ws/btcusdt@trade
            │
            ▼
       aiohttp WS
            │
      _ws_loop() — coroutine permanente
            │
      reconnexion automatique avec exponential backoff
            │
     handler(TickEvent)
```

### Reconnexion avec backoff exponentiel

```python
async def _ws_loop(self, url, handler, symbol):
    backoff = 1.0
    while self._connected:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    backoff = 1.0  # reset sur succès
                    async for msg in ws:
                        data = json.loads(msg.data)
                        if data.get("e") == "trade":
                            event = DataNormalizer.binance_ws_trade_to_event(data)
                            await handler(event)
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)  # cap à 60s
```

### Données historiques REST

```python
# GET /api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000
params = {
    "symbol":    "BTCUSDT",
    "interval":  "1h",
    "startTime": start_ms,
    "endTime":   end_ms,
    "limit":     1000,  # max Binance
}
```

Kline Binance → BarEvent :

| Index | Champ Binance | Champ BarEvent |
|---|---|---|
| 0 | Open time (ms) | `timestamp` |
| 1 | Open | `open` |
| 2 | High | `high` |
| 3 | Low | `low` |
| 4 | Close | `close` |
| 5 | Volume | `volume` |

---

## 5. HistoricalDataManager

Couche de **cache multi-niveaux** pour les données OHLCV.

### Stratégie de cache

```
Requête get_bars(symbol, timeframe, lookback=500)
    │
    ├─→ [L1] Cache mémoire dict (session courante)
    │         TTL implicite : durée de la session
    │         ✅ HIT → retourne immédiatement
    │
    ├─→ [L2] TimescaleDB (runs précédents)
    │         Query OHLCV par (symbol, timeframe, time range)
    │         ✅ HIT → retourne + peuple L1
    │
    └─→ [L3] Provider (MT5 / Binance)
              Fetch réseau → stocke en L2 → peuple L1
```

### Routing par asset class

```python
# Détection heuristique de la classe d'actif
is_crypto = any(
    symbol.upper().startswith(c)
    for c in ["BTC", "ETH", "BNB", "XRP", "SOL", "ADA", "DOT", "LINK"]
)

if is_crypto and self._binance:
    bars = await self._binance.get_historical_bars(...)
elif self._mt5:
    bars = await self._mt5.get_historical_bars(...)
```

> **Note** : En production, maintenir un registre d'instruments explicite (table `instruments` en DB) plutôt qu'une heuristique sur le symbole.

### Refresh incrémental

À chaque `BarEvent` fermée (is_closed=True), le cache est mis à jour :

```python
async def refresh(self, symbol, timeframe, new_bar: BarEvent) -> pd.DataFrame:
    cache_key = f"{symbol}:{timeframe}"
    new_row = pd.DataFrame([{...}], index=[new_bar.timestamp])

    self._memory_cache[cache_key] = pd.concat([
        self._memory_cache[cache_key],
        new_row,
    ]).tail(10_000)   # garde les 10k dernières barres en mémoire

    return self._memory_cache[cache_key]
```

---

## 6. GBM Synthetic Fallback

Pour le développement et les tests sans connexion broker, le `MT5DataProvider` peut générer des séries de prix synthétiques via un processus de **Geometric Brownian Motion** :

```
S(t+dt) = S(t) · exp( (μ - σ²/2)·dt + σ·√dt·Z )

où :
  S(t) = prix à t
  μ    = drift annualisé (0.02 = +2%/an)
  σ    = volatilité annualisée (0.15 = 15%/an)
  Z    ~ N(0,1)
  dt   = 1 / (n_bars × 252)
```

```python
def _generate_synthetic_bars(self, symbol, timeframe, count):
    np.random.seed(42)   # reproductible
    price = 1.0850
    dt    = 1 / (count * 252)
    mu, sigma = 0.02, 0.15

    for i in range(count):
        ret   = np.random.normal(mu * dt, sigma * np.sqrt(dt))
        o     = price
        price *= np.exp(ret)
        c     = price
        h     = max(o, c) * (1 + abs(np.random.normal(0, 0.0005)))
        l     = min(o, c) * (1 - abs(np.random.normal(0, 0.0005)))
        yield BarEvent(source="synthetic", ...)
```

**Limitations** : La GBM ne modélise pas les fat tails (queues épaisses), les gaps overnight, ni les effets de microstructure. Uniquement pour les tests fonctionnels.

---

## 7. Persistance TimescaleDB

Toute la persistance est gérée via `infrastructure/repositories/db_repositories.py` avec asyncpg (pas d'ORM).

### Choix asyncpg

```python
# asyncpg : binaire PostgreSQL natif, 3-5× plus rapide qu'asyncpg/psycopg2
# Pool de connexions : min=5, max=20
pool = await asyncpg.create_pool(
    dsn=settings.database.url,
    min_size=5,
    max_size=20,
    command_timeout=30,
    server_settings={"jit": "off"},  # JIT PostgreSQL inutile pour les courtes requêtes OLTP
)
```

### Bulk insert OHLCV

```python
async def save_bars_bulk(self, bars: List[BarEvent]) -> int:
    rows = [
        (b.timestamp, b.symbol, b.timeframe,
         b.open, b.high, b.low, b.close, b.volume,
         b.source, b.source == "synthetic")
        for b in bars
    ]
    async with pool.acquire() as conn:
        # executemany() utilise des prepared statements côté serveur
        # Performance: ~50,000 rows/sec sur i7 standard
        await conn.executemany(INSERT_SQL, rows)
```

### Upsert pour les replays

```sql
INSERT INTO ohlcv (time, symbol, timeframe, open, high, low, close, volume)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (time, symbol, timeframe) DO NOTHING
```

---

## 8. Schéma de base de données

### Table `ohlcv` (hypertable TimescaleDB)

```sql
CREATE TABLE ohlcv (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(32) NOT NULL,
    timeframe   VARCHAR(4)  NOT NULL,
    open        NUMERIC(20,8),
    high        NUMERIC(20,8),
    low         NUMERIC(20,8),
    close       NUMERIC(20,8),
    volume      NUMERIC(20,8),
    source      VARCHAR(16),
    is_synthetic BOOLEAN,
    PRIMARY KEY (time, symbol, timeframe)   -- clé composite
);

-- Partitionnement automatique par semaine
SELECT create_hypertable('ohlcv', 'time', chunk_time_interval => INTERVAL '1 week');

-- Compression automatique après 7 jours
SELECT add_compression_policy('ohlcv', INTERVAL '7 days');
```

### Continuous Aggregate (OHLCV daily)

```sql
-- Pré-calcul automatique du daily depuis les barres H1
CREATE MATERIALIZED VIEW ohlcv_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS day,
    symbol,
    FIRST(open,  time)         AS open,
    MAX(high)                  AS high,
    MIN(low)                   AS low,
    LAST(close,  time)         AS close,
    SUM(volume)                AS volume
FROM ohlcv
WHERE timeframe = 'H1'
GROUP BY day, symbol;
```

### Audit log (append-only)

```sql
-- Règles PostgreSQL qui empêchent toute modification
CREATE RULE audit_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;
```

---

## 9. Routing par classe d'actif

```
Symbol reçu
    │
    ├── Se termine par USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD ?
    │         → AssetClass.FOREX    → MT5DataProvider
    │
    ├── Commence par BTC, ETH, BNB, XRP, SOL, ADA, DOT, LINK ?
    │         → AssetClass.CRYPTO   → BinanceDataProvider
    │
    ├── Contient "/" ou "FUT" ou se termine par "F" ?
    │         → AssetClass.FUTURES  → MT5DataProvider
    │
    └── Autre (AAPL, SPY, NVDA...)
              → AssetClass.STOCKS   → MT5DataProvider
```

---

## 10. Considérations de production

### Latence

| Opération | Cible | Mesure |
|---|---|---|
| MT5 tick poll (100ms) | < 150 ms | Prometheus histogram `trading_market_data_latency_seconds` |
| Binance WS message | < 5 ms | idem |
| DB bulk insert (100 bars) | < 50 ms | `trading_market_data_ticks_total` |
| Cache L1 hit | < 0.1 ms | — |

### Gestion des gaps de données

En production, des mécanismes supplémentaires sont nécessaires :
- Détection des gaps (barres manquantes entre deux timestamps)
- Backfill automatique depuis l'API REST
- Alerte si gap > N barres consécutives

### Rate limits Binance

| Endpoint | Limite | Gestion |
|---|---|---|
| `/api/v3/klines` | 1200 req/min | Header `X-MBX-USED-WEIGHT` à surveiller |
| WebSocket | 5 connexions/IP | 1 connexion par symbole max |

### Sécurité des credentials

```python
# Les credentials ne sont jamais dans le code
# Injectés via environment variables
class MT5Settings(BaseSettings):
    password: SecretStr    # masqué dans les logs

# Accès
settings.mt5.password.get_secret_value()  # explicite
```

---

*Document précédent → [01_ARCHITECTURE.md](01_ARCHITECTURE.md)*  
*Document suivant → [03_STRATEGY_ENGINE.md](03_STRATEGY_ENGINE.md)*
