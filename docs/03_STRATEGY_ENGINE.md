# 03 — Strategy Engine

> **Niveau** : Quant Researchers, Senior Engineers  
> **Fichiers** : `indicator_engine/service.py`, `strategy_engine/service.py`

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Indicator Engine — Architecture Numba](#2-indicator-engine--architecture-numba)
3. [Kernels JIT disponibles](#3-kernels-jit-disponibles)
4. [IndicatorService — cache et cycle de vie](#4-indicatorservice--cache-et-cycle-de-vie)
5. [Strategy Plugin Registry](#5-strategy-plugin-registry)
6. [BaseStrategy — cycle de vie](#6-basestrategy--cycle-de-vie)
7. [EMACrossoverStrategy — logique détaillée](#7-emacrossovestrategy--logique-détaillée)
8. [StrategyEngine — fan-out et routing](#8-strategyengine--fan-out-et-routing)
9. [Ajouter une stratégie custom](#9-ajouter-une-stratégie-custom)
10. [Performance et benchmarks](#10-performance-et-benchmarks)

---

## 1. Vue d'ensemble

La couche Stratégie est composée de **deux sous-systèmes** couplés mais séparés :

```
BarEvent (fermée)
      │
      ▼
┌─────────────────────────────────────────────────┐
│               INDICATOR ENGINE                  │
│                                                 │
│  ┌──────────────┐    ┌──────────────────────┐   │
│  │  Numba JIT   │    │  IndicatorService     │   │
│  │   Kernels    │───▶│  Cache (sym, tf, ind) │   │
│  │ (pure numpy) │    │  get_last_value()     │   │
│  └──────────────┘    └──────────────────────┘   │
└─────────────────────────────┬───────────────────┘
                              │ DataFrame enrichi
                              ▼
┌─────────────────────────────────────────────────┐
│               STRATEGY ENGINE                   │
│                                                 │
│  ┌──────────────┐    ┌──────────────────────┐   │
│  │   Registry   │    │    StrategyEngine     │   │
│  │ (Plugin Map) │    │  fan-out → on_bar()  │   │
│  └──────────────┘    └──────────────────────┘   │
│                               │                 │
│     ┌─────────────────────────┤                 │
│     │  BaseStrategy instances │                 │
│     │  EMACrossoverStrategy   │                 │
│     └─────────────────────────┘                 │
└─────────────────────────────┬───────────────────┘
                              │
                        SignalEvent
                    (LONG / SHORT / FLAT)
```

---

## 2. Indicator Engine — Architecture Numba

### Pourquoi Numba ?

Python pur et pandas sont trop lents pour les calculs d'indicateurs sur des fenêtres glissantes :

| Implémentation | EMA(200 bars) | RSI(200 bars) |
|---|---|---|
| Pandas `.ewm()` | ~2.1 ms | ~3.8 ms |
| NumPy pur | ~0.9 ms | ~1.5 ms |
| **Numba JIT** | **~0.08 ms** | **~0.12 ms** |

La différence est de **10-25×**. Sur 100 stratégies tournant chaque seconde, c'est critique.

### Architecture à deux niveaux

```
Niveau 1 — Kernels Numba (@njit)
  Input  : numpy.ndarray (raw float64)
  Output : numpy.ndarray
  État   : aucun — fonctions pures
  JIT    : compilé à la première appel, cached sur disque

Niveau 2 — Wrappers Python (compute_ema, compute_rsi...)
  Input  : pd.DataFrame (OHLCV nommé)
  Output : pd.Series (même index que df)
  But    : extraire les arrays, appeler le kernel, reconstruire la Series
```

```python
# Niveau 1 : kernel Numba — opère uniquement sur des arrays C-continus
@njit(cache=True)
def _ema_kernel(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out   = np.empty(len(values), dtype=np.float64)
    out[:] = np.nan
    # Seed SMA → puis lissage exponentiel
    seed = np.mean(values[:period])
    out[period - 1] = seed
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out

# Niveau 2 : wrapper — pont entre pandas et Numba
def compute_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    arr    = df[column].to_numpy(dtype=np.float64)   # copy C-contiguous
    result = _ema_kernel(arr, period)                # Numba JIT
    return pd.Series(result, index=df.index, name=f"EMA_{period}")
```

### Premier appel : JIT compilation

```
Appel 1 : ~800 ms (compilation LLVM)
Appel 2 : ~0.08 ms (cache disque)
Appel N : ~0.08 ms (cache mémoire)

→ Le Dockerfile inclut un warm-up au build time pour éliminer la latence du 1er appel en prod.
```

---

## 3. Kernels JIT disponibles

### EMA — Exponential Moving Average

**Formule** : `EMA(t) = α · Close(t) + (1 - α) · EMA(t-1)` où `α = 2/(n+1)`

**Seed** : SMA des `n` premières valeurs (convention Wilder)

```python
ema_9  = compute_ema(df, period=9)    # fast
ema_21 = compute_ema(df, period=21)   # slow
ema_50 = compute_ema(df, period=50)   # medium-term trend
```

---

### RSI — Relative Strength Index

**Formule** :
```
RS  = avg_gain / avg_loss  (lissage Wilder sur n périodes)
RSI = 100 - 100 / (1 + RS)
```

**Propriétés** : toujours dans [0, 100]. RSI constant = 50 si pas de variation de prix.

```python
rsi = compute_rsi(df, period=14)
# Seuils standards : <30 = survendu, >70 = suracheté
```

---

### ATR — Average True Range

**Formule** :
```
TR  = max(H-L, |H-Prev_C|, |L-Prev_C|)
ATR = Wilder_smooth(TR, n)
```

Utilisé comme filtre de volatilité dans les stratégies (ne pas trader si ATR trop faible).

---

### MACD

```
MACD Line   = EMA(12) - EMA(26)
Signal Line = EMA(9) de MACD Line
Histogram   = MACD Line - Signal Line
```

Retourne un `pd.DataFrame` avec colonnes : `macd_line`, `signal_line`, `histogram`.

---

### Bollinger Bands

```
Mid   = SMA(20)
Upper = Mid + 2 × std(20)
Lower = Mid - 2 × std(20)
Width = (Upper - Lower) / Mid
```

`bb_position = (Close - Mid) / (2 × std)` → mesure où le prix se situe dans les bandes.

---

### Stochastic Oscillator

```
%K = (Close - LowestLow_n) / (HighestHigh_n - LowestLow_n) × 100
%D = SMA(3) de %K
```

Niveaux : < 20 = survendu, > 80 = suracheté.

---

### VWAP — Volume Weighted Average Price

```
VWAP = Σ(TypicalPrice × Volume) / Σ(Volume)
     = Σ((H+L+C)/3 × V) / Σ(V)
```

Session-based (reset quotidien). Non compilé en Numba car nécessite un `GroupBy` par date.

---

### ADX — Average Directional Index

Mesure la **force** d'une tendance (pas la direction).

```
+DM, -DM → smoothés sur n → +DI, -DI
DX = 100 × |+DI - -DI| / (+DI + -DI)
ADX = EMA(DX, n)
```

| ADX | Interprétation |
|---|---|
| < 20 | Marché sans tendance |
| 20–40 | Tendance émergente |
| 40–60 | Tendance forte |
| > 60 | Tendance très forte (rare) |

---

## 4. IndicatorService — cache et cycle de vie

### Clé de cache

```python
_CacheKey = Tuple[str, str, str]
# ("EURUSD", "H1", "EMA_9")
```

### API

```python
# Calculer et cacher tous les indicateurs d'un coup
results = ind_svc.compute_all(
    symbol    = "EURUSD",
    timeframe = "H1",
    df        = bars_df,
    config    = {"ema_periods": [9, 21, 50], "rsi_period": 14},
)

# Lire la dernière valeur
ema9_val = ind_svc.get_last_value("EURUSD", "H1", "EMA_9")   # float ou None
ema9_ser = ind_svc.get("EURUSD", "H1", "EMA_9")              # pd.Series complète

# Invalider (ex: changement de timeframe)
ind_svc.invalidate("EURUSD", "H1")
```

### Politique d'éviction LRU

```python
# Si plus de 500 clés en cache : supprimer les 100 plus anciennes
if len(self._cache) > self._max_cache:
    keys_to_remove = list(self._cache.keys())[:100]
    for k in keys_to_remove:
        del self._cache[k]
```

> **Note** : En production, utiliser `functools.lru_cache` ou Redis avec TTL = durée de la barre.

---

## 5. Strategy Plugin Registry

Le registry est un **Pattern Plugin** : les stratégies s'auto-déclarent à l'import.

```python
# Dans strategy_engine/service.py
class StrategyRegistry:
    _registry: dict[str, tuple[Type[BaseStrategy], dict]] = {}

    def register(self, strategy_id: str, default_config: dict = None):
        def decorator(cls):
            self._registry[strategy_id] = (cls, default_config or {})
            return cls
        return decorator

    def create(self, strategy_id: str, config: dict = None) -> BaseStrategy:
        cls, defaults = self._registry[strategy_id]
        merged = {**defaults, **(config or {})}
        return cls(config=merged)

    def list_registered(self) -> list[str]:
        return list(self._registry.keys())

# Singleton global
StrategyRegistry = StrategyRegistry()
```

### Enregistrement

```python
@StrategyRegistry.register(
    "ema_crossover_v1",
    default_config={"fast_period": 9, "slow_period": 21}
)
class EMACrossoverStrategy(BaseStrategy):
    ...
```

### Création dynamique

```python
# En production (via container)
strategy = StrategyRegistry.create("ema_crossover_v1", config={"fast_period": 5})

# En backtest (via API /backtest/run)
strategy_cls = StrategyRegistry.get("ema_crossover_v1")
instance     = strategy_cls(config=request.strategy_config)
```

---

## 6. BaseStrategy — cycle de vie

```python
class BaseStrategy(ABC):
    def __init__(self, config: dict = None):
        self._config       = config or {}
        self._bar_history  = deque(maxlen=500)   # rolling buffer
        self._indicators   = {}                   # attached indicators
        self.is_active     = True
        self._last_signal  = None

    # Méthode principale — implémenter dans les sous-classes
    @abstractmethod
    async def on_bar(self, bar: BarEvent, ind: IndicatorService) -> SignalEvent | None:
        ...

    def attach_indicator(self, name, func) -> "BaseStrategy":
        """Fluent builder pour attacher des indicateurs."""
        self._indicators[name] = func
        return self

    def reset(self) -> None:
        """Réinitialise l'état pour un backtest propre."""
        self._bar_history.clear()
        self._last_signal = None
        self.is_active = True

    def activate(self) -> None:
        self.is_active = True

    def deactivate(self) -> None:
        self.is_active = False
```

### Détection de changement de direction

```python
def _direction_changed(self, new_direction: str) -> bool:
    """Évite d'émettre des signaux redondants dans la même direction."""
    if self._last_signal is None:
        return True
    changed = self._last_signal.direction != new_direction
    if changed:
        self._last_signal = None
    return changed
```

---

## 7. EMACrossoverStrategy — logique détaillée

### Paramètres

| Paramètre | Défaut | Description |
|---|---|---|
| `fast_period` | 9 | Période EMA rapide |
| `slow_period` | 21 | Période EMA lente |
| `atr_period` | 14 | Période ATR (filtre volatilité) |
| `min_atr_pct` | 0.0005 | ATR minimum pour trader (0.05%) |
| `symbols` | `["EURUSD"]` | Instruments suivis |

### Logique de signal

```
Conditions d'entrée LONG :
  [1] EMA_fast(t-1) ≤ EMA_slow(t-1)   (était en dessous)
  [2] EMA_fast(t)   > EMA_slow(t)     (vient de croiser au-dessus)
  [3] ATR > prix × min_atr_pct        (volatilité suffisante)
  [4] Direction != LONG déjà émis     (pas de doublon)

Conditions d'entrée SHORT :
  [1] EMA_fast(t-1) ≥ EMA_slow(t-1)
  [2] EMA_fast(t)   < EMA_slow(t)
  [3] ATR > prix × min_atr_pct
  [4] Direction != SHORT déjà émis

Signal FLAT :
  Émis automatiquement lors d'un croisement inverse
  (fermeture de position)
```

### Calcul de la force du signal (strength)

```python
# Gap relatif entre les deux EMAs → conviction
ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow
strength    = min(ema_gap_pct / 0.005, 1.0)   # normalisé [0, 1]
# Plus les EMAs sont écartées, plus la conviction est haute
```

### Code complet

```python
async def on_bar(self, bar: BarEvent, ind: IndicatorService) -> SignalEvent | None:
    if not self.is_active or bar.symbol not in self.symbols:
        return None

    fast_k = f"EMA_{self._config['fast_period']}"
    slow_k = f"EMA_{self._config['slow_period']}"
    atr_k  = f"ATR_{self._config['atr_period']}"

    fast_series = ind.get(bar.symbol, bar.timeframe, fast_k)
    slow_series = ind.get(bar.symbol, bar.timeframe, slow_k)

    if fast_series is None or len(fast_series.dropna()) < 2:
        return None

    fast_curr = float(fast_series.iloc[-1])
    fast_prev = float(fast_series.iloc[-2])
    slow_curr = float(slow_series.iloc[-1])
    slow_prev = float(slow_series.iloc[-2])

    # Filtre ATR
    atr_val = ind.get_last_value(bar.symbol, bar.timeframe, atr_k) or 0
    if atr_val < bar.close * self._config.get("min_atr_pct", 0.0005):
        return None

    # Croisement haussier
    if fast_prev <= slow_prev and fast_curr > slow_curr:
        if self._direction_changed("LONG"):
            strength = min(abs(fast_curr - slow_curr) / slow_curr / 0.005, 1.0)
            return SignalEvent(
                source=self.strategy_id, strategy_id=self.strategy_id,
                symbol=bar.symbol, direction="LONG",
                strength=strength, signal_price=bar.close,
                timeframe=bar.timeframe,
            )

    # Croisement baissier
    if fast_prev >= slow_prev and fast_curr < slow_curr:
        if self._direction_changed("SHORT"):
            strength = min(abs(fast_curr - slow_curr) / slow_curr / 0.005, 1.0)
            return SignalEvent(
                source=self.strategy_id, strategy_id=self.strategy_id,
                symbol=bar.symbol, direction="SHORT",
                strength=strength, signal_price=bar.close,
                timeframe=bar.timeframe,
            )

    return None
```

---

## 8. StrategyEngine — fan-out et routing

### Index par symbole (O(1) routing)

```python
class StrategyEngine:
    def __init__(self):
        self._strategies  = {}            # strategy_id → BaseStrategy
        self._symbol_idx  = defaultdict(list)  # symbol → [strategy_ids]

    def register_strategy(self, strategy: BaseStrategy) -> None:
        self._strategies[strategy.strategy_id] = strategy
        for sym in strategy.symbols:
            self._symbol_idx[sym].append(strategy.strategy_id)
```

### Fan-out concurrent

```python
async def on_bar(self, bar: BarEvent) -> None:
    strategy_ids = self._symbol_idx.get(bar.symbol, [])

    # Toutes les stratégies tournent en parallèle sur la même barre
    tasks = [
        self._strategies[sid].on_bar(bar, self._indicator_svc)
        for sid in strategy_ids
        if self._strategies[sid].is_active
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, SignalEvent):
            # Validation risk avant publication
            validation = await self._risk_engine.validate_signal(result, ...)
            if validation.approved:
                await self._event_bus.publish(result)
```

---

## 9. Ajouter une stratégie custom

```python
# fichier : strategy_engine/strategies/rsi_mean_reversion.py

from strategy_engine.service import BaseStrategy, StrategyRegistry
from core.domain.events import BarEvent, SignalEvent
from indicator_engine.service import IndicatorService


@StrategyRegistry.register(
    "rsi_mean_reversion_v1",
    default_config={
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "symbols": ["EURUSD", "GBPUSD"],
    },
)
class RSIMeanReversionStrategy(BaseStrategy):
    """
    Signal LONG  quand RSI < oversold  (survendu → rebond attendu).
    Signal SHORT quand RSI > overbought (suracheté → repli attendu).
    """

    @property
    def strategy_id(self) -> str:
        return "rsi_mean_reversion_v1"

    @property
    def symbols(self) -> list[str]:
        return self._config["symbols"]

    async def on_bar(self, bar: BarEvent, ind: IndicatorService) -> SignalEvent | None:
        period = self._config["rsi_period"]
        rsi_val = ind.get_last_value(bar.symbol, bar.timeframe, f"RSI_{period}")

        if rsi_val is None:
            return None

        if rsi_val < self._config["oversold"] and self._direction_changed("LONG"):
            return SignalEvent(
                source=self.strategy_id, strategy_id=self.strategy_id,
                symbol=bar.symbol, direction="LONG",
                strength=(self._config["oversold"] - rsi_val) / self._config["oversold"],
                signal_price=bar.close, timeframe=bar.timeframe,
            )

        if rsi_val > self._config["overbought"] and self._direction_changed("SHORT"):
            return SignalEvent(
                source=self.strategy_id, strategy_id=self.strategy_id,
                symbol=bar.symbol, direction="SHORT",
                strength=(rsi_val - self._config["overbought"]) / (100 - self._config["overbought"]),
                signal_price=bar.close, timeframe=bar.timeframe,
            )

        return None
```

Pour activer cette stratégie :

```python
# Dans container.py
from strategy_engine.strategies.rsi_mean_reversion import RSIMeanReversionStrategy

rsi_strategy = StrategyRegistry.create("rsi_mean_reversion_v1")
container.strategy_engine.register_strategy(rsi_strategy)
```

---

## 10. Performance et benchmarks

### Cibles de latence (post-JIT warm-up)

| Opération | Cible p99 | Mesure Prometheus |
|---|---|---|
| EMA(200 bars) | < 0.1 ms | `test_benchmarks.py` |
| RSI(200 bars) | < 0.1 ms | idem |
| Suite complète (200 bars) | < 5 ms | `trading_strategy_signal_latency_seconds` |
| `on_bar()` complet | < 1 ms | idem |

### Résultats benchmarks (`test_benchmarks.py`)

```bash
pytest tests/unit/test_benchmarks.py -v -s

PASSED test_ema_200bars_under_5ms         [ EMA(200 bars): 0.082ms avg ]
PASSED test_rsi_200bars_under_5ms         [ RSI(200 bars): 0.115ms avg ]
PASSED test_full_indicator_suite_under_50ms [ Suite complète: 3.8ms avg ]
```

### Profiling Numba

```python
# Activer le profiling Numba
import numba
numba.config.NUMBA_ENABLE_PROFILING = 1

# Inspecter le code LLVM généré
from indicator_engine.service import _ema_kernel
print(_ema_kernel.inspect_llvm())
```

---

*Document précédent → [02_DATA_ENGINE.md](02_DATA_ENGINE.md)*  
*Document suivant → [04_RISK_ENGINE.md](04_RISK_ENGINE.md)*
