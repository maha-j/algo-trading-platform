# 12 — Stratégie de Tests

> **Niveau** : Senior Engineers, QA Engineers, Lead Developers  
> **Fichiers** : `tests/`, `tests/conftest.py`, `tests/unit/test_core.py`, `tests/unit/test_benchmarks.py`, `tests/integration/test_integration.py`, `pyproject.toml`

---

## Table des matières

1. [Philosophie et pyramide de tests](#1-philosophie-et-pyramide-de-tests)
2. [Organisation des tests](#2-organisation-des-tests)
3. [Configuration pytest](#3-configuration-pytest)
4. [Fixtures partagées (conftest.py)](#4-fixtures-partagées-conftestpy)
5. [Tests unitaires — inventaire complet](#5-tests-unitaires--inventaire-complet)
6. [Tests d'intégration](#6-tests-dintégration)
7. [Tests de performance (benchmarks)](#7-tests-de-performance-benchmarks)
8. [Property-based testing avec Hypothesis](#8-property-based-testing-avec-hypothesis)
9. [Stratégie de mocking](#9-stratégie-de-mocking)
10. [Coverage et qualité](#10-coverage-et-qualité)
11. [Intégration CI/CD](#11-intégration-cicd)
12. [Guide pour écrire de nouveaux tests](#12-guide-pour-écrire-de-nouveaux-tests)

---

## 1. Philosophie et pyramide de tests

### Principes fondamentaux

Dans une plateforme de trading institutionnelle, les tests ne sont pas optionnels. Chaque couche de la plateforme a des exigences de qualité différentes :

```
                        ┌───────────┐
                        │   E2E     │  2–5 tests — scénarios complets
                       /│  Tests    │\   (paper trading simulé)
                      / └───────────┘ \
                     /                  \
                    /  ┌─────────────┐   \
                   /   │ Integration │    \
                  /    │   Tests     │     \  20–30 tests — Redis, signal flow
                 /     └─────────────┘      \
                /                            \
               / ┌────────────────────────┐   \
              /  │      Unit Tests         │    \
             /   │  45+ tests, zéro I/O    │     \
            /    └────────────────────────┘      \
           /                                      \
          / ┌──────────────────────────────────┐   \
         /  │        Benchmarks                │    \
         \  │  Latency & throughput assertions │    /
          \ └──────────────────────────────────┘   /
           \__________________________________________/
```

### Règle d'or : les tests unitaires ne touchent jamais le réseau

```python
# ✅ Test unitaire correct
async def test_portfolio_pnl(fill_event_factory):
    engine = PortfolioEngine(initial_capital=100_000.0)   # pur Python
    fill   = fill_event_factory("EURUSD", "BUY", 10000, 1.0850)
    await engine.on_fill(fill)
    assert engine.get_equity() > 0   # calcul arithmétique, pas de DB

# ❌ Ce n'est pas un test unitaire
async def test_portfolio_persists():
    await db.execute("SELECT 1")  # connexion réseau = test d'intégration
```

### Niveaux de confiance par composant

| Composant | Criticité financière | Coverage cible |
|---|---|---|
| `risk_engine` | Critique | 95% |
| `portfolio_engine` | Critique | 95% |
| `execution_engine` | Critique | 90% |
| `indicator_engine` | Haute | 85% |
| `strategy_engine` | Haute | 85% |
| `backtest_engine` | Moyenne | 80% |
| `ml_engine` | Moyenne | 75% |
| `notification` | Basse | 70% |
| `dashboard` | Basse | 60% |

---

## 2. Organisation des tests

```
tests/
│
├── conftest.py                    ← Fixtures partagées (session-scoped)
│
├── unit/                          ← Tests purs, zéro I/O externe
│   ├── __init__.py
│   ├── test_core.py               ← 45+ tests (events, portfolio, indicators, risk, backtest)
│   └── test_benchmarks.py         ← Assertions de latence et throughput
│
├── integration/                   ← Requièrent Redis (pas de PostgreSQL nécessaire)
│   ├── __init__.py
│   └── test_integration.py        ← Redis roundtrip, signal flow, full backtest
│
└── e2e/                           ← (À créer) — scénarios complets
    └── test_paper_trading.py
```

### Conventions de nommage

```
test_{module}_{scenario}_{expected_outcome}

Exemples :
  test_portfolio_buy_creates_position
  test_rsi_constant_price_is_50
  test_circuit_breaker_trip_and_reset
  test_backtest_nextbar_fill_no_lookahead_bias
  test_ema_200bars_under_5ms
```

---

## 3. Configuration pytest

### `pyproject.toml` — section pytest

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"              # Toutes les coroutines s'exécutent automatiquement
testpaths    = ["tests"]
addopts      = """
    --cov=.
    --cov-report=term-missing
    --cov-report=html
    --cov-omit=tests/*,migrations/*
    --tb=short
    -q
"""
markers = [
    "unit:        unit tests (no external dependencies)",
    "integration: integration tests (require Redis)",
    "slow:        slow tests (backtest > 1000 bars, ML training)",
    "benchmark:   performance benchmarks with latency assertions",
]
```

### Commandes de lancement

```bash
# Tous les tests unitaires (rapides, < 30s)
pytest tests/unit -m "not slow" -v

# Tests d'intégration (requiert Redis local)
pytest tests/integration -m integration -v

# Benchmarks avec affichage des mesures
pytest tests/unit/test_benchmarks.py -v -s

# Un seul test (debugging)
pytest tests/unit/test_core.py::TestPortfolioEngine::test_realised_pnl_after_close -v

# Avec coverage HTML
pytest tests/unit --cov=. --cov-report=html
# → Ouvrir htmlcov/index.html

# Tout sauf les tests lents
pytest tests/ -m "not slow and not integration" -q

# Tout avec toutes les options
pytest tests/ -v --tb=long

# Via Makefile
make test-unit        # Rapides
make test-integration # Avec Redis
make test-bench       # Benchmarks
make coverage         # Coverage HTML
```

---

## 4. Fixtures partagées (conftest.py)

Les fixtures sont dans `tests/conftest.py`. Les fixtures session-scoped sont calculées **une fois** pour toute la suite de tests.

### `gbm_bars_1000` — série GBM partagée

```python
@pytest.fixture(scope="session")
def gbm_bars_1000() -> pd.DataFrame:
    """
    1000 barres OHLCV synthétiques GBM.
    Scope session = calculé une seule fois pour toute la suite.
    Coût : ~10ms de calcul NumPy.
    """
    np.random.seed(42)   # Reproductible — même seed = même résultat
    n = 1000
    prices = [1.0850]
    for _ in range(n - 1):
        prices.append(prices[-1] * np.exp(np.random.normal(0, 0.001)))

    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open":   [p * 0.9998 for p in prices],
        "high":   [p * 1.0005 for p in prices],
        "low":    [p * 0.9995 for p in prices],
        "close":  prices,
        "volume": np.random.uniform(500, 3000, n),
    }, index=idx)
```

### `mock_settings` — settings sans fichier .env

```python
@pytest.fixture
def mock_settings():
    """
    MagicMock qui prévient le chargement du vrai .env en CI.
    Toutes les valeurs risk/execution/notification sont configurées ici.
    """
    s = MagicMock()
    s.risk.max_position_size_pct = 5.0
    s.risk.max_open_positions    = 10
    s.risk.max_daily_loss_pct    = 3.0
    s.risk.max_drawdown_pct      = 15.0
    s.risk.var_confidence        = 0.99
    s.api.jwt_secret.get_secret_value.return_value = "test-secret-32-chars-minimum!!!!!"
    return s
```

### `portfolio_100k` — engine fresh avec $100k

```python
@pytest.fixture
def portfolio_100k():
    from portfolio_engine.service import PortfolioEngine
    return PortfolioEngine(initial_capital=100_000.0)
```

### `indicator_service` — service avec cache vide

```python
@pytest.fixture
def indicator_service():
    from indicator_engine.service import IndicatorService
    return IndicatorService()   # Cache vide à chaque test
```

### `fill_event_factory` — factory d'événements de fill

```python
@pytest.fixture
def fill_event_factory():
    def _make(symbol="EURUSD", side="BUY", qty=1.0, price=1.0850, commission=7.0):
        from core.domain.events import FillEvent
        return FillEvent(
            source="test", order_id=str(uuid4()),
            symbol=symbol, side=side, quantity=qty,
            fill_price=price, commission=commission,
        )
    return _make
```

---

## 5. Tests unitaires — inventaire complet

### `TestDomainEvents` (5 tests)

| Test | Vérification |
|---|---|
| `test_tick_event_is_frozen` | `frozen=True` — toute modification lève une exception |
| `test_tick_mid_spread_properties` | `mid = (bid+ask)/2`, `spread = ask-bid` |
| `test_bar_event_channel` | Canal Redis correct (`stream:bars`) |
| `test_signal_event_unique_ids` | Deux instances ont des `event_id` différents (UUID v4) |
| `test_order_event_defaults` | `risk_approved=False` par défaut |

### `TestPortfolioEngine` (8 tests)

| Test | Vérification |
|---|---|
| `test_initial_state` | Equity = capital initial, 0 positions, drawdown = 0 |
| `test_buy_creates_position` | Un BUY crée une position long avec net_qty correct |
| `test_full_close_removes_position` | BUY puis SELL même qty → position supprimée |
| `test_realised_pnl_after_close` | `(1.0900-1.0850)×10000 - 14 commission = 36.00` |
| `test_short_position_pnl` | SELL puis BUY de couverture → profit correct |
| `test_unrealised_pnl_updates_on_tick` | `on_tick()` met à jour le P&L non-réalisé |
| `test_drawdown_calculation` | Drawdown > 0 après une baisse de prix |
| `test_position_sizer_vol_target` | Taille ≤ 5% de l'equity |
| `test_position_sizer_kelly_zero_edge` | Win rate < 50% → Kelly négatif → taille = 0 |

### `TestIndicatorEngine` (9 tests)

| Test | Vérification |
|---|---|
| `test_ema_convergence` | EMA converge vers le prix moyen ±5% |
| `test_ema_length_matches_input` | `len(ema) == len(df)` (alignement d'index) |
| `test_rsi_bounds` | RSI ∈ [0, 100] sur toute la série |
| `test_rsi_constant_price_is_50` | Prix constant → RSI = 50 (gains = pertes) |
| `test_atr_positive` | ATR ≥ 0 partout |
| `test_bollinger_bands_ordering` | `upper ≥ mid ≥ lower` partout |
| `test_macd_histogram_is_diff` | `histogram == macd_line - signal_line` exactement |
| `test_stochastic_bounds` | `%K ∈ [0, 100]` |
| `test_indicator_service_caches` | Après `compute_all()`, `get()` retourne une valeur |
| `test_indicator_service_invalidate` | Après `invalidate()`, `get()` retourne None |

### `TestRiskEngine` (6 tests)

| Test | Vérification |
|---|---|
| `test_valid_signal_passes` | Signal normal → `approved=True` |
| `test_drawdown_breach_rejects` | Drawdown 20% > limite 15% → `approved=False` |
| `test_circuit_breaker_blocks_all_signals` | CB ouvert → tout bloqué indépendamment |
| `test_circuit_breaker_trip_and_reset` | Trip → is_open=True, reset → is_open=False |
| `test_var_calculation` | VaR > 0 et CVaR ≥ VaR (propriété mathématique) |
| `test_empty_returns_returns_zero_var` | Pas de données → VaR = 0.0 (pas d'exception) |

### `TestBacktestStatistics` (6 tests)

| Test | Vérification |
|---|---|
| `test_sharpe_positive_returns` | Returns positifs constants → Sharpe > 0 |
| `test_negative_returns_negative_sharpe` | Returns négatifs → Sharpe < 0 |
| `test_max_drawdown_flat_curve` | Courbe plate → max drawdown = 0% |
| `test_max_drawdown_known_value` | Pic 110k → creux 99k → DD = -10% (±0.5%) |
| `test_profit_factor_known` | 200 USD gains / 50 USD pertes → PF = 4.0 |
| `test_win_rate_calculation` | 3 trades gagnants / 4 total → win rate = 75% |

### `TestMLEngine` (4 tests)

| Test | Vérification |
|---|---|
| `test_feature_engine_shape` | `len(features) == len(df)`, même index |
| `test_feature_engine_no_inf` | Aucune valeur infinie dans les features |
| `test_target_no_lookahead` | Target correctement décalée (shift) |
| `test_regime_classifier_output_range` | Régime ∈ {0, 1, 2, 3} |

### `TestDataNormalizer` (2 tests)

| Test | Vérification |
|---|---|
| `test_mt5_tick_to_event` | Tick brut → TickEvent avec champs corrects |
| `test_ohlcv_to_dataframe_sorted` | DataFrame triée par timestamp ASC |

---

## 6. Tests d'intégration

Les tests d'intégration requièrent **Redis uniquement** (pas PostgreSQL). Ils valident le comportement réel end-to-end sans mocks.

### Détection automatique de Redis

```python
# tests/integration/test_integration.py
REDIS_AVAILABLE = False
try:
    import redis as redis_sync
    client = redis_sync.Redis(host="localhost", port=6379, socket_connect_timeout=2)
    client.ping()
    REDIS_AVAILABLE = True
except Exception:
    pass

skip_no_redis = pytest.mark.skipif(
    not REDIS_AVAILABLE,
    reason="Redis not available — skipping integration tests"
)
```

### `TestRedisEventBus` (2 tests)

```python
@skip_no_redis
class TestRedisEventBus:

    async def test_publish_and_consume_roundtrip(self):
        """
        Publie un TickEvent sur Redis Streams, vérifie que le consumer le reçoit.
        Teste XADD + XREADGROUP + handler callback.
        """
        received = []
        async def handler(msg): received.append(msg)

        bus = RedisEventBus(client, group="test_grp", consumer_name="test_consumer")
        await bus.register_handler("stream:ticks:test", handler)

        tick = TickEvent(source="test", symbol="EURUSD", bid=1.085, ask=1.0851, volume=1.0)
        await bus.publish(tick)

        consume_task = asyncio.create_task(bus.start_consuming())
        await asyncio.sleep(0.5)
        consume_task.cancel()

        assert len(received) >= 1

    async def test_publish_many_batch(self):
        """
        publish_many() doit envoyer 50 events en un pipeline Redis.
        Vérifie que le count retourné est correct.
        """
        events = [BarEvent(...) for i in range(50)]
        count  = await bus.publish_many(events)
        assert count == 50
```

### `TestSignalFlowIntegration` (2 tests)

```python
class TestSignalFlowIntegration:
    """Tests in-process (sans Redis) — le bus est mocké."""

    async def test_ema_crossover_generates_signal(self, synthetic_bars):
        """
        La stratégie doit générer ≥ 1 signal sur 1000 barres.
        Vérifie que direction ∈ {LONG, SHORT, FLAT} et strength ∈ [0, 1].
        """
        strategy  = EMACrossoverStrategy(config={"fast_period": 9, "slow_period": 21})
        ind_svc   = IndicatorService()
        signals   = []

        for i, (ts, row) in enumerate(synthetic_bars.iterrows()):
            bar    = BarEvent(source="test", ...)
            sub_df = synthetic_bars.iloc[max(0, i-100): i+1]
            ind_svc.compute_all("EURUSD", "H1", sub_df)
            signal = await strategy.on_bar(bar, ind_svc)
            if signal:
                signals.append(signal)
            if len(signals) >= 3:
                break

        assert len(signals) >= 1
        for s in signals:
            assert s.direction in ("LONG", "SHORT", "FLAT")
            assert 0.0 <= s.strength <= 1.0

    async def test_portfolio_tracks_multiple_fills(self, synthetic_bars):
        """
        P&L net après 3 round-trips :
          BUY 1.0850 → SELL 1.0900 : +50 - 14 = +36
          BUY 1.0900 → SELL 1.0850 : -50 - 14 = -64
          SELL 1.0950 → BUY 1.0900 : +50 - 14 = +36
          Total : +8 USD
        """
        portfolio = PortfolioEngine(initial_capital=100_000.0)
        # ... 6 fills
        assert float(portfolio.get_realised_pnl()) == pytest.approx(8.0, abs=1.0)
        assert len(portfolio.get_positions()) == 0   # toutes positions fermées
```

### `TestBacktestEngineIntegration` (3 tests)

| Test | Description |
|---|---|
| `test_full_backtest_on_synthetic_data` | Backtest complet 1000 barres → stats valides |
| `test_walk_forward_produces_multiple_splits` | WFO 3 splits → 3 résultats OOS |
| `test_backtest_next_bar_fill_model` | Timestamps des fills = barres suivantes |

### `TestNotificationService` (2 tests)

| Test | Description |
|---|---|
| `test_queue_and_deliver_alert` | Alert enfilée → délivrée au channel mock |
| `test_circuit_breaker_suppresses_channel` | 3 failures → breaker ouvert → delivery=False |

---

## 7. Tests de performance (benchmarks)

Les benchmarks vérifient que les **cibles de latence** définies dans le budget de performance sont respectées.

### `TestIndicatorBenchmarks`

```python
def test_ema_200bars_under_5ms(self):
    from indicator_engine.service import compute_ema
    compute_ema(BARS_200, 20)   # warm-up JIT

    t0 = time.perf_counter()
    for _ in range(100):
        compute_ema(BARS_200, 20)
    elapsed_ms = (time.perf_counter() - t0) / 100 * 1000

    print(f"\n  EMA(200 bars): {elapsed_ms:.3f}ms avg")
    assert elapsed_ms < 5.0, f"EMA trop lent: {elapsed_ms:.2f}ms (limite 5ms)"
```

### Tableau des cibles

| Test | Limite | Composant |
|---|---|---|
| `test_ema_200bars_under_5ms` | < 5 ms | `_ema_kernel` Numba |
| `test_rsi_200bars_under_5ms` | < 5 ms | `_rsi_kernel` Numba |
| `test_full_indicator_suite_under_50ms` | < 100 ms | `IndicatorService.compute_all()` |
| `test_indicator_service_1000bars` | < 500 ms | Suite sur 1000 barres |
| `test_on_fill_latency_under_0_5ms` | < 2 ms | `PortfolioEngine.on_fill()` |
| `test_backtest_1000bars_under_10s` | < 10 s | Backtest complet 1000 barres |
| `test_backtest_throughput_bars_per_second` | > 200 bars/s | Throughput backtest |

### Méthode de mesure

```python
# Mesure sur N=100 répétitions → moyenne
# Évite les cold-start JVM/GC effects
t0 = time.perf_counter()
for _ in range(100):
    result = function_under_test()
elapsed_ms = (time.perf_counter() - t0) / 100 * 1000
```

> **Note** : Les benchmarks sont conçus pour tourner après le warm-up Numba. En CI, la première exécution est lente (~800ms pour EMA) — c'est pourquoi chaque test inclut un appel de warm-up avant la mesure.

---

## 8. Property-based testing avec Hypothesis

`hypothesis` est installé dans les dépendances dev. Il permet de tester des **propriétés mathématiques** plutôt que des exemples fixes.

### Exemple : propriétés RSI

```python
from hypothesis import given, strategies as st, settings
from hypothesis.extra.numpy import arrays

@given(
    prices=arrays(
        dtype=float,
        shape=st.integers(min_value=20, max_value=500),
        elements=st.floats(min_value=0.0001, max_value=10000.0,
                          allow_nan=False, allow_infinity=False),
    ),
    period=st.integers(min_value=5, max_value=50),
)
@settings(max_examples=100)
def test_rsi_always_in_bounds(prices, period):
    """RSI doit toujours être dans [0, 100], quelle que soit l'entrée."""
    df  = pd.DataFrame({"close": prices})
    rsi = compute_rsi(df, period).dropna()
    assert (rsi >= 0).all()
    assert (rsi <= 100).all()
```

### Exemple : propriétés VaR

```python
@given(
    returns=arrays(
        dtype=float,
        shape=st.integers(min_value=30, max_value=1000),
        elements=st.floats(min_value=-0.5, max_value=0.5,
                          allow_nan=False, allow_infinity=False),
    ),
)
def test_cvar_always_geq_var(returns):
    """CVaR ≥ VaR est une propriété mathématique universelle."""
    var  = _compute_historical_var(pd.Series(returns), 0.99)
    cvar = _compute_cvar(pd.Series(returns), 0.99)
    assert cvar >= var - 1e-10   # tolérance numérique
```

---

## 9. Stratégie de mocking

### Principe : mocker au niveau de la frontière de couche

```python
# ✅ Correct : mocker l'interface, pas l'implémentation
class FakePortfolio:
    """Double de test complet — remplace la vraie implémentation."""
    async def get_equity(self) -> Decimal:
        return Decimal("100000")
    async def get_positions(self) -> dict:
        return {}
    async def get_realised_pnl(self) -> Decimal:
        return Decimal("0")

# Dans le test
risk_engine = RiskEngine(portfolio=FakePortfolio(), ...)

# ❌ Mauvais : mocker un détail interne
with patch("risk_engine.service.VaRValidator._compute_historical_var"):
    ...  # Fragilise les tests si on renomme la méthode
```

### Mocks standards utilisés

#### `AsyncMock` pour les coroutines

```python
mock_bus = AsyncMock()
mock_bus.publish = AsyncMock()
mock_bus.start   = AsyncMock(return_value=True)
```

#### `MagicMock` pour les settings

```python
settings = MagicMock()
settings.risk.max_drawdown_pct = 15.0
```

#### `patch` pour les imports conditionnels

```python
# Tester le comportement sans MT5 installé
with patch("market_data.service.MT5DataProvider._mt5_connect", return_value=False):
    provider = MT5DataProvider(...)
    result   = await provider.connect()
    assert result is False
```

#### Factory fixtures pour les événements

```python
@pytest.fixture
def fill_event_factory():
    def _make(symbol="EURUSD", side="BUY", qty=1.0, price=1.0850, commission=7.0):
        return FillEvent(
            source="test", order_id=str(uuid4()),
            symbol=symbol, side=side,
            quantity=qty, fill_price=price, commission=commission,
        )
    return _make

# Usage
async def test_my_scenario(fill_event_factory):
    fill = fill_event_factory("BTCUSDT", "SELL", 0.1, 48500.0, 0.5)
    ...
```

---

## 10. Coverage et qualité

### Lancer le rapport de coverage

```bash
# Terminal
pytest tests/unit --cov=. --cov-report=term-missing

# HTML (plus lisible)
pytest tests/unit --cov=. --cov-report=html
open htmlcov/index.html

# Fail si < 80%
pytest tests/unit --cov=. --cov-fail-under=80
```

### Configuration d'exclusion

```toml
# pyproject.toml
[tool.coverage.run]
omit = [
    "tests/*",
    "*/migrations/*",
    "dashboard/app.py",   # UI — difficile à tester unitairement
    "*/conftest.py",
]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "@abstractmethod",
]
```

### Ignorer les lignes non-testables

```python
def _emergency_handler():  # pragma: no cover
    """Gestionnaire de signal OS — non testable en unit test standard."""
    sys.exit(1)
```

### Objectifs par module

```
core/domain/events.py          → 100% (critique, trivial à tester)
core/interfaces/__init__.py    → 100% (Protocols)
portfolio_engine/service.py    → ≥ 95%
risk_engine/service.py         → ≥ 95%
execution_engine/service.py    → ≥ 90%
indicator_engine/service.py    → ≥ 85%
strategy_engine/service.py     → ≥ 85%
backtest_engine/service.py     → ≥ 80%
ml_engine/service.py           → ≥ 75%
notification/service.py        → ≥ 70%
```

---

## 11. Intégration CI/CD

### GitHub Actions — configuration complète

```yaml
# .github/workflows/tests.yml
name: Test Suite

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  unit-tests:
    name: Unit Tests
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python 3.12
        uses: actions/setup-python@v4
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Lint (ruff)
        run: ruff check .

      - name: Type check (mypy)
        run: mypy core/ indicator_engine/ portfolio_engine/ risk_engine/ --ignore-missing-imports

      - name: Unit tests
        run: pytest tests/unit -m "not slow" --tb=short -q

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          files: ./coverage.xml

  integration-tests:
    name: Integration Tests
    runs-on: ubuntu-latest

    services:
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Integration tests
        env:
          REDIS_HOST: localhost
          REDIS_PORT: 6379
        run: pytest tests/integration -m integration --tb=short -v

  benchmarks:
    name: Performance Benchmarks
    runs-on: ubuntu-latest
    # Ne fail pas le build — juste pour la visibilité
    continue-on-error: true

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: "3.12"

      - name: Install
        run: pip install -e ".[dev]"

      - name: Run benchmarks
        run: pytest tests/unit/test_benchmarks.py -v -s
```

### Pre-commit hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.2.2
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.8.0
    hooks:
      - id: mypy
        args: [--ignore-missing-imports]
        additional_dependencies: [pydantic]
```

```bash
# Installer les hooks
pre-commit install

# Tester manuellement
pre-commit run --all-files
```

---

## 12. Guide pour écrire de nouveaux tests

### Checklist pour chaque nouveau test

```
1. Le test a un nom descriptif (test_{composant}_{scenario}_{résultat_attendu})
2. Il y a exactement UN assert principal (principe AAA : Arrange, Act, Assert)
3. Aucun sleep() dans les tests unitaires
4. Aucun appel réseau dans les tests unitaires (tout est mocké)
5. Le test est déterministe (même seed NumPy, même résultat)
6. Async tests utilisent le décorateur @pytest.mark.asyncio
7. Les fixtures sont utilisées plutôt que des objects créés inline
8. Les cas limites sont couverts (empty list, zero equity, etc.)
```

### Template d'un test unitaire

```python
class TestMyComponent:
    """Tests pour MyComponent."""

    # --- Fixtures locales si nécessaire ---
    @pytest.fixture
    def my_component(self, mock_settings):
        with patch("my_module.get_settings", return_value=mock_settings):
            from my_module import MyComponent
            return MyComponent()

    # --- Tests normaux (happy path) ---
    async def test_normal_scenario(self, my_component):
        # Arrange
        input_data = create_test_input()

        # Act
        result = await my_component.process(input_data)

        # Assert
        assert result.status == "OK"
        assert result.value == pytest.approx(expected, abs=0.01)

    # --- Tests de cas limites ---
    async def test_empty_input(self, my_component):
        result = await my_component.process([])
        assert result is None   # ou toute autre valeur attendue

    # --- Tests d'erreur ---
    async def test_invalid_input_raises(self, my_component):
        with pytest.raises(ValueError, match="positive"):
            await my_component.process(negative_value=-1)
```

### Template d'un test d'intégration

```python
@skip_no_redis
class TestMyIntegration:
    """Tests d'intégration — requièrent Redis."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_flow(self):
        # Setup réel (pas de mock)
        bus      = RedisEventBus(redis_client, ...)
        received = []

        await bus.register_handler("stream:test", lambda m: received.append(m))

        # Act
        await bus.publish(create_test_event())
        await asyncio.sleep(0.3)

        # Assert
        assert len(received) == 1
```

### Anti-patterns à éviter

```python
# ❌ Test non-déterministe (dépend du timing)
async def test_bad():
    await asyncio.sleep(0.1)   # Race condition possible
    assert something_happened()

# ✅ Correct : attendre explicitement avec timeout
async def test_good():
    result = await asyncio.wait_for(wait_for_event(), timeout=1.0)
    assert result is not None

# ❌ Test trop large (plusieurs assertions non liées)
async def test_too_much():
    engine = PortfolioEngine(100_000)
    fill   = FillEvent(...)
    await engine.on_fill(fill)
    assert engine.get_equity() == ...
    assert len(engine.get_positions()) == 1
    assert engine.get_realised_pnl() == 0
    # ... 10 autres assertions

# ✅ Correct : un test = une responsabilité
async def test_buy_creates_position(portfolio_engine, fill_event_factory):
    fill = fill_event_factory("EURUSD", "BUY", 10000, 1.0850)
    await portfolio_engine.on_fill(fill)
    assert "EURUSD" in portfolio_engine.get_positions()
```

---

## Récapitulatif

```
tests/unit/test_core.py          → 45+ tests, 0 I/O externe
tests/unit/test_benchmarks.py    → 8 assertions de latence/throughput
tests/integration/test_integration.py → 12 tests (Redis requis)
tests/conftest.py                → 6 fixtures partagées session-scoped

Commande de référence CI :
  pytest tests/unit -m "not slow" -q && echo "✅ Tests OK"

Commande de référence local (tout) :
  make test-unit && make test-integration && make test-bench
```

---

*Document précédent → [11_SECURITY.md](11_SECURITY.md)*  
*Fin de la documentation — 12 fichiers générés.*
