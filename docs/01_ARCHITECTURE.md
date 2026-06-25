# 01 — Architecture Générale

> **Niveau** : Architectes, Lead Engineers  
> **Objectif** : Comprendre la structure globale, les principes directeurs et les décisions de design qui régissent l'ensemble de la plateforme.

---

## Table des matières

1. [Vision et objectifs](#1-vision-et-objectifs)
2. [Clean Architecture](#2-clean-architecture)
3. [Structure des couches](#3-structure-des-couches)
4. [Flux de données principal](#4-flux-de-données-principal)
5. [Patterns architecturaux](#5-patterns-architecturaux)
6. [Protocoles et interfaces](#6-protocoles-et-interfaces)
7. [Injection de dépendances](#7-injection-de-dépendances)
8. [Bus d'événements](#8-bus-dévénements)
9. [Structure du projet](#9-structure-du-projet)
10. [Décisions architecturales (ADR)](#10-décisions-architecturales-adr)

---

## 1. Vision et objectifs

La plateforme est conçue selon les standards des fonds quantitatifs institutionnels (Citadel, Two Sigma, Renaissance). Elle vise :

| Objectif | Cible |
|---|---|
| **Latence tick→signal** | < 5 ms (p99) |
| **Latence signal→ordre** | < 10 ms (p99) |
| **Throughput backtest** | > 500 bars/sec |
| **Disponibilité** | 99.9 % (hors maintenance broker) |
| **Assets supportés** | Forex, Crypto, Stocks, Futures |
| **Précision financière** | `Decimal` partout — zéro flottant sur les prix |

---

## 2. Clean Architecture

La plateforme suit strictement la **Clean Architecture** de Robert Martin. Les dépendances ne pointent **que vers l'intérieur**.

```
╔══════════════════════════════════════════════════════════════╗
║                     INFRASTRUCTURE                           ║
║   Docker · PostgreSQL · Redis · Prometheus · FastAPI         ║
║  ┌────────────────────────────────────────────────────────┐  ║
║  │                   APPLICATION                          │  ║
║  │  Strategy · Risk · Execution · Portfolio · Backtest    │  ║
║  │  ┌──────────────────────────────────────────────────┐  │  ║
║  │  │                  DOMAIN                          │  │  ║
║  │  │   Events · Interfaces · Value Objects            │  │  ║
║  │  └──────────────────────────────────────────────────┘  │  ║
║  └────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════╝
```

### Règle d'or

```python
# ✅ Correct — les engines ne connaissent que les interfaces du domaine
from core.interfaces import IDataProvider, IRiskEngine

# ❌ Interdit — jamais d'import de broker dans une couche applicative
from execution_engine.service import MT5BrokerAdapter  # UNIQUEMENT dans container.py
```

---

## 3. Structure des couches

```
trading_platform/
│
├── core/                          ← DOMAINE (aucune dépendance externe)
│   ├── domain/
│   │   └── events.py              ← Frozen dataclasses (immutables)
│   └── interfaces/
│       └── __init__.py            ← 9 Protocols PEP 544
│
├── config/
│   └── settings.py                ← Pydantic BaseSettings (12-factor)
│
├── market_data/                   ← COUCHE DONNÉES
│   └── service.py                 ← MT5Provider, BinanceProvider, Normalizer
│
├── indicator_engine/              ← CALCUL TECHNIQUE
│   └── service.py                 ← Kernels Numba JIT + IndicatorService cache
│
├── strategy_engine/               ← LOGIQUE DE SIGNAL
│   └── service.py                 ← Registry, BaseStrategy, EMACrossover
│
├── portfolio_engine/              ← ÉTAT DU PORTEFEUILLE
│   └── service.py                 ← Positions, P&L Decimal, Kelly sizer
│
├── risk_engine/                   ← CONTRÔLE DU RISQUE
│   └── service.py                 ← Chain of Responsibility + CircuitBreaker
│
├── execution_engine/              ← ROUTAGE DES ORDRES
│   └── service.py                 ← MARKET/TWAP/VWAP + MT5BrokerAdapter
│
├── backtest_engine/               ← SIMULATION HISTORIQUE
│   └── service.py                 ← Next-bar fill + statistiques complètes
│
├── ml_engine/                     ← INTELLIGENCE ARTIFICIELLE
│   └── service.py                 ← GMM Regime, LightGBM Predictor, IsolationForest
│
├── monitoring/                    ← OBSERVABILITÉ
│   └── metrics.py                 ← Prometheus singleton + JSON logging
│
├── notification/                  ← ALERTES
│   └── service.py                 ← Telegram, SMTP, Webhook + routing
│
├── api/                           ← INTERFACE HTTP
│   ├── main.py                    ← FastAPI app, WS, middleware
│   └── routers.py                 ← 6 routers: portfolio, strategy, orders...
│
├── dashboard/                     ← INTERFACE VISUELLE
│   └── app.py                     ← Streamlit 6 pages
│
├── infrastructure/                ← COUCHE TECHNIQUE
│   ├── container.py               ← Composition Root (DI manuel)
│   ├── event_bus/
│   │   └── redis_event_bus.py     ← Redis Streams (XADD/XREADGROUP)
│   └── repositories/
│       └── db_repositories.py     ← asyncpg OHLCV, fills, audit log
│
├── migrations/
│   └── init.sql                   ← TimescaleDB schema
│
├── docker/
│   ├── Dockerfile                 ← Multi-stage build
│   ├── docker-compose.yml         ← 16 services
│   ├── prometheus.yml
│   └── alert_rules.yml
│
└── tests/
    ├── unit/                      ← 45+ tests, zéro I/O externe
    └── integration/               ← Redis roundtrip, signal flow
```

---

## 4. Flux de données principal

Le cycle de vie complet d'un signal, de la donnée brute à l'exécution :

```
 Broker/Exchange
      │
      │ tick (100ms poll / WebSocket)
      ▼
 ┌─────────────────┐
 │  DataProvider   │  MT5DataProvider (ThreadPoolExecutor)
 │                 │  BinanceProvider (aiohttp WebSocket)
 └────────┬────────┘
          │ TickEvent / BarEvent
          ▼
 ┌─────────────────┐
 │ Redis Streams   │  XADD → stream:ticks / stream:bars
 │  (Event Bus)    │  Durabilité + replay + consumer groups
 └────────┬────────┘
          │ on_bar() — bar fermée seulement
          ▼
 ┌─────────────────┐
 │ IndicatorEngine │  Numba JIT: EMA, RSI, ATR, MACD, BB, ADX
 │                 │  Cache (symbol, timeframe, indicator_name)
 └────────┬────────┘
          │ DataFrame enrichi
          ▼
 ┌─────────────────┐
 │ StrategyEngine  │  Fan-out vers toutes les stratégies abonnées
 │                 │  Plugin registry + direction change detection
 └────────┬────────┘
          │ SignalEvent (LONG / SHORT / FLAT)
          ▼
 ┌─────────────────┐
 │   RiskEngine    │  Chain of Responsibility (5 validateurs)
 │                 │  CircuitBreaker → publie RiskBreachEvent si KO
 └────────┬────────┘
          │ signal approuvé
          ▼
 ┌─────────────────┐
 │ PortfolioEngine │  calculate_position_size() → Decimal
 │                 │  Kelly Criterion blended avec Vol-Targeting
 └────────┬────────┘
          │ OrderEvent (qty calculée, risk_approved=True)
          ▼
 ┌─────────────────┐
 │ ExecutionEngine │  MARKET / TWAP / VWAP selon l'algo
 │                 │  MT5BrokerAdapter (ThreadPoolExecutor)
 └────────┬────────┘
          │ FillEvent
          ▼
 ┌─────────────────┐
 │ PortfolioEngine │  on_fill() → mise à jour positions/P&L
 │                 │  Decimal FIFO/VWAP cost basis
 └─────────────────┘
          │
          ├─→ Redis stream:fills → persistance DB
          ├─→ Prometheus metrics
          └─→ Notification (si seuil alerte)
```

---

## 5. Patterns architecturaux

### 5.1 Observer / Event-Driven

Tous les composants communiquent via des **événements immuables** publiés sur Redis Streams. Aucun engine n'appelle directement un autre engine.

```python
# Les événements sont des frozen dataclasses — immuables après création
@dataclass(frozen=True, slots=True)
class SignalEvent(BaseEvent):
    direction: str       # "LONG" | "SHORT" | "FLAT"
    strength: float      # 0.0 → 1.0
    signal_price: float
```

### 5.2 Chain of Responsibility (Risk)

```
Signal → PositionLimitValidator
       → MaxOpenPositionsValidator
       → DailyLossValidator
       → VaRValidator
       → DrawdownValidator
       → ✅ APPROUVÉ  ou  ❌ REJETÉ (fail-fast)
```

### 5.3 Strategy Pattern (Exécution)

```python
# L'algorithme est choisi à l'exécution sans modifier le code client
algo = {
    "MARKET": MarketOrderAlgorithm(),
    "TWAP":   TWAPAlgorithm(slices=10, duration_sec=300),
    "VWAP":   VWAPAlgorithm(),
}[order.algorithm]
fills = await algo.execute(order, broker)
```

### 5.4 Plugin Registry (Stratégies)

```python
@StrategyRegistry.register("ema_crossover_v1")
class EMACrossoverStrategy(BaseStrategy):
    ...

# Instantiation dynamique sans import explicite
strategy = StrategyRegistry.create("ema_crossover_v1", config={...})
```

### 5.5 Circuit Breaker (Trading halt)

```
État CLOSED  ──(breach critique)──→  État OPEN
     ↑                                    │
     └──────(reset manuel admin)──────────┘

OPEN = toutes les validations Risk retournent False
```

### 5.6 Repository Pattern (Persistance)

```python
# Le domaine ne sait pas que c'est PostgreSQL
class OHLCVRepository:
    async def save_bars_bulk(self, bars: List[BarEvent]) -> int: ...
    async def get_bars(self, symbol, timeframe, start, end) -> List[dict]: ...
```

### 5.7 Composition Root (DI Container)

```python
# container.py est le SEUL endroit où les concrétions sont nommées
class TradingPlatformContainer:
    @property
    def risk_engine(self) -> RiskEngine:
        return RiskEngine(
            portfolio=self.portfolio_engine,   # dépendance injectée
            event_bus=self.event_bus,
        )
```

---

## 6. Protocoles et interfaces

La plateforme définit **9 Protocols PEP 544** dans `core/interfaces/__init__.py`. Tous sont `@runtime_checkable`.

| Protocol | Implémentations concrètes |
|---|---|
| `IDataProvider` | `MT5DataProvider`, `BinanceDataProvider` |
| `IIndicator` | `compute_ema`, `compute_rsi`, `compute_atr`... |
| `IStrategy` | `EMACrossoverStrategy`, toute classe custom |
| `IRiskEngine` | `RiskEngine` |
| `IExecutionEngine` | `ExecutionEngine` |
| `IPortfolioEngine` | `PortfolioEngine` |
| `IBacktestEngine` | `BacktestEngine` |
| `IMLModel` | `ReturnPredictor`, `RegimeClassifier` |
| `INotificationChannel` | `TelegramChannel`, `EmailChannel`, `WebhookChannel` |

**Pourquoi Protocol et non ABC ?**

```python
# Avec Protocol : duck typing structurel
# Une librairie tierce peut satisfaire l'interface sans hériter

class MyCustomProvider:
    async def connect(self) -> bool: ...
    async def get_historical_bars(self, ...) -> pd.DataFrame: ...

assert isinstance(MyCustomProvider(), IDataProvider)  # ✅ True — structural check
```

---

## 7. Injection de dépendances

Le container est **manuel** (pas de framework). Avantages sur `python-dependency-injector` :

- Lisibilité : chaque dépendance visible à la lecture
- Performance : zéro overhead de réflexion
- Testabilité : override par sous-classe dans les tests

```python
# Démarrage de la plateforme (entrypoint)
container = TradingPlatformContainer()
await container.start()

# Ordre de démarrage obligatoire :
# 1. event_bus  → 2. broker  → 3. portfolio  → 4. risk
# → 5. execution  → 6. strategy  → 7. wire subscriptions
```

### Override pour les tests

```python
class TestContainer(TradingPlatformContainer):
    @property
    def broker(self):
        return FakeBroker()   # Remplace MT5 sans changer le code prod
```

---

## 8. Bus d'événements

**Redis Streams** (pas Pub/Sub) pour la durabilité et le replay.

| Feature | Redis Pub/Sub | Redis Streams (choix) |
|---|---|---|
| Messages persistés | ❌ | ✅ |
| Replay possible | ❌ | ✅ |
| Consumer groups | ❌ | ✅ |
| Acknowledgement | ❌ | ✅ |
| Backpressure | ❌ | ✅ (MAXLEN) |

```
Stream: stream:bars
  ├── Group: trading-core
  │     └── Consumer: trading-core-1   ← XREADGROUP, XACK
  └── Group: backtest
        └── Consumer: backtest-1       ← Replay indépendant
```

### Canaux définis

| Canal | Producteur | Consommateurs |
|---|---|---|
| `stream:ticks` | MT5/Binance | IndicatorEngine, Monitoring |
| `stream:bars` | DataProvider | StrategyEngine |
| `stream:signals` | StrategyEngine | RiskEngine → ExecutionEngine |
| `stream:orders` | ExecutionEngine | Broker |
| `stream:fills` | ExecutionEngine | PortfolioEngine, DB, Notify |
| `stream:risk` | RiskEngine | Notifications, Dashboard |

---

## 9. Structure du projet

```
trading_platform/
├── docs/                 ← Cette documentation (12 fichiers)
├── core/                 ← Aucune dépendance pip
├── config/               ← Pydantic settings (1 fichier)
├── *_engine/             ← Un module par couche métier
├── infrastructure/       ← Détails techniques (Redis, asyncpg)
├── api/                  ← FastAPI (adapters inbound)
├── dashboard/            ← Streamlit (adapters inbound)
├── monitoring/           ← Prometheus + logging
├── notification/         ← Telegram, SMTP, Webhook
├── docker/               ← 16 services complets
├── migrations/           ← TimescaleDB schema + hypertables
└── tests/
    ├── unit/             ← 45+ tests, mocks uniquement
    ├── integration/      ← Requiert Redis
    └── conftest.py       ← Fixtures partagées
```

---

## 10. Décisions architecturales (ADR)

### ADR-001 : `Decimal` pour tous les prix

**Décision** : Utiliser `decimal.Decimal` pour tous les calculs financiers.

**Contexte** : `float` en IEEE-754 introduit des erreurs d'arrondi cumulatives :
```python
>>> 0.1 + 0.2 == 0.3
False
>>> Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
True
```

**Conséquence** : Performance légèrement inférieure aux flottants (acceptable car ce n'est pas dans la hot-path des calculs Numba).

---

### ADR-002 : Protocol (PEP 544) vs ABC

**Décision** : Protocol pour toutes les interfaces.

**Raison** : Duck typing structurel, pas de couplage d'héritage, compatible avec des adaptateurs tiers sans modification.

---

### ADR-003 : Redis Streams vs Kafka

**Décision** : Redis Streams pour le bus d'événements.

**Raison** : Kafka est optimal pour > 1 M messages/sec. Pour une plateforme traitant < 100 k events/sec, Redis Streams offre :
- Opérations simples (même infra que le cache)
- Latence p99 < 1 ms
- Pas de JVM, pas de ZooKeeper

---

### ADR-004 : ThreadPoolExecutor pour MT5

**Décision** : Isoler toutes les appels MT5 dans un `ThreadPoolExecutor`.

**Raison** : L'API Python MT5 est synchrone (wrapping d'une DLL C++). Appeler directement depuis asyncio bloquerait toute la plateforme pendant chaque appel réseau.

```python
result = await loop.run_in_executor(self._executor, self._mt5_connect)
```

---

### ADR-005 : DI container manuel vs framework

**Décision** : Container DI écrit à la main dans `infrastructure/container.py`.

**Raison** : `python-dependency-injector` et `injector` ajoutent une indirection de réflexion. Un container manuel est :
- Lisible directement (aucune magie)
- Vérifiable par mypy
- 0 overhead à l'exécution

---

### ADR-006 : TimescaleDB vs InfluxDB

**Décision** : TimescaleDB (extension PostgreSQL) pour toutes les séries temporelles.

**Raison** :
- SQL natif : les requêtes d'analyse P&L utilisent des JOINs classiques
- Compression automatique des chunks > 7 jours
- Continuous aggregates : OHLCV daily pré-calculée sans ETL séparé
- Un seul moteur DB pour les données transactionnelles ET time-series

---

*Prochain document → [02_DATA_ENGINE.md](02_DATA_ENGINE.md)*
