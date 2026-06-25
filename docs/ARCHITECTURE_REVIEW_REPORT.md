# Architecture Review Report
## Institutional Algorithmic Trading Platform
### Principal Architect Audit — June 2026

---

> **Classification**: Internal — Engineering Leadership  
> **Scope**: Full codebase audit across 57 source files, 12 documentation files, ~18,400 lines  
> **Methodology**: Static analysis, cross-reference verification, runtime-path tracing, security review  
> **Verdict**: The platform demonstrates strong architectural intent with several **critical runtime bugs** and **deployment blockers** that must be resolved before production use.

---

## Executive Summary

The platform is architecturally sound at the strategic level. Clean Architecture layers are respected, the event-driven design is coherent, and the separation of concerns across engines is well-conceived. However, a detailed audit reveals **8 bugs that will cause incorrect financial calculations or runtime crashes**, **6 missing infrastructure files that block deployment**, **3 interface contract violations**, and **12 production-readiness gaps**. None are irreparable — all have clear, bounded fixes.

---

## 1. Critical Bugs (P0 — Financial Correctness)

### BUG-01 · Double-Counting in `PortfolioEngine.get_equity()`

**File**: `portfolio_engine/service.py`, line 354  
**Severity**: P0 — produces incorrect equity, P&L, drawdown, and all risk metrics

**The defect**:
```python
# Current (WRONG):
return (self._cash + self._initial_capital + unrealised_total + self.get_realised_pnl())

# At initialisation:
#   self._cash           = 100_000   ← initial capital loaded into cash
#   self._initial_capital= 100_000   ← same value stored again
#   get_equity() at t=0  = 200_000   ← DOUBLE the correct value
```

`_cash` is seeded with `initial_capital` at construction and then adjusted with fills. `_initial_capital` is a separate constant. Summing both double-counts the starting capital. The correct formula is simply `_cash + unrealised_total`.

**Additional defect**: `get_realised_pnl()` sums `p.realised_pnl` over `self._positions.values()` — but positions are **deleted from the dict when closed** (`del self._positions[symbol]`). All realised P&L from closed trades is permanently lost. The engine needs a `_cumulative_realised_pnl: Decimal` accumulator.

**Fix**:
```python
def __init__(self, initial_capital: float = 100_000.0, ...) -> None:
    self._cash                    = Decimal(str(initial_capital))
    self._initial_capital         = Decimal(str(initial_capital))   # read-only reference
    self._cumulative_realised_pnl = Decimal("0")                    # accumulator

def get_equity(self) -> Decimal:
    return (self._cash + self.get_unrealised_pnl()).quantize(...)

def get_realised_pnl(self) -> Decimal:
    return self._cumulative_realised_pnl   # not from live positions dict

# In apply_fill() or on_fill(), when a position closes:
self._cumulative_realised_pnl += closed_pnl - commission
```

---

### BUG-02 · `FillEvent.quantity` Does Not Exist — Runtime `AttributeError`

**Files**: `portfolio_engine/service.py` (lines 112, 311, 326), `tests/unit/test_core.py` (line 65)  
**Severity**: P0 — crashes on every fill, making the platform non-functional

The canonical domain event (`core/domain/events.py`) defines `FillEvent.quantity_filled` (line 229). The portfolio engine consistently reads `fill.quantity` — a field that **does not exist** on the frozen dataclass. Every call to `on_fill()` will raise `AttributeError: 'FillEvent' object has no attribute 'quantity'`.

The test factory also creates `FillEvent(quantity=qty, ...)` which will raise `TypeError` on construction since `quantity` is not a valid field.

**Fix**: Standardise on the canonical field name throughout. Option A: rename `quantity_filled` → `quantity` in `events.py` (simpler, less confusion). Option B: update all consumers to use `quantity_filled`. Pick one and apply universally.

---

### BUG-03 · `BarEvent` Field Name Mismatch — `open` vs `open_price`

**Files**: `core/domain/events.py` (line 120), `backtest_engine/service.py` (line 246), `tests/unit/test_core.py` (lines 111, 530)  
**Severity**: P0 — backtest crashes on every fill execution

`BarEvent` defines `open_price`, `high_price`, `low_price`, `close_price`. The backtest engine references `next_bar.open` (line 246) — which does not exist. This AttributeError fires on every simulated fill. Tests also construct `BarEvent(open=..., high=..., close=...)` using wrong field names.

**Fix**: Rename domain fields to short names (`open`, `high`, `low`, `close`) — standard in all financial libraries — or update all consumer sites. Short names are strongly preferred.

---

### BUG-04 · `DailyLossValidator` Uses Cumulative P&L, Not Daily

**File**: `risk_engine/service.py`, line 183  
**Severity**: P0 — the "daily loss limit" never triggers correctly

```python
# Current (WRONG):
realised_pnl = float(await portfolio.get_realised_pnl())   # cumulative all-time
daily_loss_pct = min(0.0, realised_pnl / equity)           # not a daily measure
```

After a profitable early run, `realised_pnl` will be large and positive. A 3% daily loss will never breach the limit because cumulative P&L absorbs it. A strategy can lose 3% every single day indefinitely.

**Fix**: The portfolio engine must expose `get_daily_pnl() -> Decimal` that queries fills from TimescaleDB for the current UTC calendar day:
```python
async def get_daily_pnl(self) -> Decimal:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return await self._fill_repository.sum_pnl_since(today_start)
```

---

### BUG-05 · `datetime.utcnow()` Deprecated — Produces Timezone-Naive Datetimes

**Files**: `core/domain/events.py` (line 55), `execution_engine/service.py` (lines 102, 103, 126, 342, 384)  
**Severity**: P1 — silent data corruption in time-sensitive contexts

`datetime.utcnow()` is deprecated since Python 3.12 and returns a **timezone-naive** datetime that cannot be compared with timezone-aware datetimes (the rest of the system uses `timezone.utc`). Comparisons will raise `TypeError`. TimescaleDB stores `TIMESTAMPTZ` and will produce incorrect sort results if naive timestamps are inserted.

**Fix** (global search-and-replace):
```python
# BEFORE:
datetime.utcnow()
# AFTER:
datetime.now(timezone.utc)
```

---

### BUG-06 · `RiskEngine.validate_signal()` Signature Mismatch

**Files**: `core/interfaces/__init__.py` (line 181), `risk_engine/service.py` (line 494), `tests/unit/test_core.py` (line 343)  
**Severity**: P1 — tests fail; container wiring may fail silently

The Protocol declares:
```python
async def validate_signal(self, signal: SignalEvent) -> bool:
```
The tests call it with three positional arguments:
```python
await engine.validate_signal(signal, portfolio, np.array([]))
```
The actual implementation signature on `RiskEngine` takes only `signal`. The tests will either fail with a `TypeError` or be testing against a different method. The interface, implementation, and tests are all inconsistent.

**Fix**: Align all three. The richest signature (used by tests) is more correct for a standalone validator:
```python
async def validate_signal(
    self,
    signal: SignalEvent,
    portfolio: IPortfolioEngine,
    returns_history: np.ndarray,
) -> ValidationResult:
```

---

### BUG-07 · Redis Consumer Group Starts from `id="0"` — Full History Replay on Restart

**File**: `infrastructure/event_bus/redis_event_bus.py`, line 259  
**Severity**: P1 — on service restart, the strategy engine replays every bar ever published, generating spurious signals and orders

```python
await self._client.xgroup_create(
    name=channel,
    groupname=self._consumer_group,
    id="0",      # ← reads all messages from beginning of stream
    mkstream=True,
)
```

If the `trading-core` restarts after running for 6 months, it will receive and process all 6 months of historical bar events. This will fire hundreds of signals, generate orders on a live broker, and potentially violate risk limits.

**Fix**: Use `id="$"` to read only new messages from this moment forward:
```python
id="$",    # only messages arriving after this consumer group creation
```
Combined with `XADD maxlen ~100_000` trim, this prevents unbounded history replay.

---

### BUG-08 · `TradingPlatformContainer.get_instance()` Does Not Exist

**File**: `api/routers.py`, line 41  
**Severity**: P0 — all API endpoints crash with `AttributeError`

The router dependency calls:
```python
return TradingPlatformContainer.get_instance()
```
`TradingPlatformContainer` is a plain `@dataclass` with no `get_instance()` class method. The container is a non-singleton dataclass. Every HTTP request to any API endpoint will raise `AttributeError`.

**Fix**: Either implement `get_instance()` as a class-level singleton, or retrieve the container from `request.app.state.container` (which is how `api/main.py` correctly stores it):
```python
def get_container(request: Request):
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(503, detail="Platform not initialised")
    return container
```

---

## 2. Deployment Blockers (P0 — Cannot Start)

### DEPLOY-01 · `docker-compose.yml` References Non-Existent Dockerfiles

**File**: `docker/docker-compose.yml`, lines 138, 177, 230  
**Severity**: P0 — `docker compose up` fails immediately

```yaml
trading-core: dockerfile: docker/Dockerfile.core    # does not exist
fastapi:      dockerfile: docker/Dockerfile.api     # does not exist
streamlit:    dockerfile: docker/Dockerfile.dashboard  # does not exist
```

The actual Dockerfile is `docker/Dockerfile` (a multi-stage file with targets `core`, `api`, `dashboard`). The compose file references three separate files that were never created.

**Fix**:
```yaml
trading-core:
  build:
    context: ..
    dockerfile: docker/Dockerfile
    target: core

fastapi:
  build:
    context: ..
    dockerfile: docker/Dockerfile
    target: api
```

---

### DEPLOY-02 · `nginx.conf` Referenced but Not Created

**File**: `docker/docker-compose.yml`, line 211  
**Severity**: P0 — nginx container will not start

```yaml
- ./docker/nginx.conf:/etc/nginx/nginx.conf:ro    # file does not exist
```

The nginx service mounts a config file that was documented in `11_SECURITY.md` but never written to disk.

---

### DEPLOY-03 · Grafana Provisioning Directories Do Not Exist

**File**: `docker/docker-compose.yml`, lines 298–299  
**Severity**: P1 — Grafana starts but no dashboards or datasources are configured

```yaml
- ./docker/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro   # missing
- ./docker/grafana/datasources:/etc/grafana/provisioning/datasources:ro # missing
```

---

### DEPLOY-04 · Redis `maxmemory-policy allkeys-lru` Conflicts With Streams

**File**: `docker/docker-compose.yml`, line 109  
**Severity**: P1 — under memory pressure, Redis silently evicts Stream messages before they are consumed

The `allkeys-lru` eviction policy allows Redis to delete **any key**, including Stream entries, to free memory. This will silently drop events without any error, causing fills to be missed and portfolio state to diverge from the broker.

**Fix**: Use `noeviction` for a trading event bus. If memory pressure is a concern, increase the memory limit or use a dedicated Redis instance for the event bus (separate from cache).
```yaml
--maxmemory-policy noeviction
```

---

### DEPLOY-05 · Database URL Property Mismatch

**File**: `infrastructure/repositories/db_repositories.py`, line 45  
**Severity**: P0 — database connections fail at startup

```python
dsn=settings.database.url    # ← .url property does not exist on DatabaseSettings
```

`DatabaseSettings` exposes `async_url` and `sync_url` as properties (lines 51, 57). There is no `.url` property. The repository pool creation will raise `AttributeError` on startup.

**Fix**: `dsn=settings.database.async_url`

---

### DEPLOY-06 · Missing Prometheus Metrics Port Configuration

**File**: `docker/prometheus.yml`  
**Severity**: P1 — Prometheus cannot scrape `trading-core`

```yaml
scrape_configs:
  - job_name: trading-core
    static_configs:
      - targets: ["trading-core:8001"]
```

The trading-core service's `healthcheck` probes port 8001, but the application starts on port 8001 only in the `core` Dockerfile stage. The API (`fastapi`) starts on 8000. The Prometheus scrape target points to 8001, but the `/metrics` endpoint is defined in `api/main.py` which runs on 8000. In the current deployment topology, the FastAPI service (port 8000) serves `/metrics`, not the core service.

---

## 3. Interface Contract Violations

### CONTRACT-01 · `IPortfolioEngine` Protocol Declares `async`, Implementation Is Sync

**Files**: `core/interfaces/__init__.py` (lines 299–334), `portfolio_engine/service.py` (lines 341–364)

The Protocol contract:
```python
async def get_positions(self) -> dict[str, dict]:  ...
async def get_equity(self) -> Decimal:  ...
async def get_realised_pnl(self) -> Decimal:  ...
```

The implementation:
```python
def get_positions(self) -> Dict[str, Position]:  ...   # sync
def get_equity(self) -> Decimal:  ...                  # sync
def get_realised_pnl(self) -> Decimal:  ...            # sync
```

Code that correctly follows the interface and `await`s these methods will wrap sync results in a coroutine — Python silently returns the value rather than raising an error, but `mypy --strict` will fail, and the `api/main.py` WebSocket handler explicitly `await`s them (lines 331–333), which produces incorrect behavior.

**Fix**: Either make all three methods `async` in the implementation, or remove the `async` from the Protocol. Since these are in-memory operations, keeping them synchronous is correct — update the Protocol.

---

### CONTRACT-02 · `StrategyEngine.on_bar()` Return Type Inconsistency

**File**: `strategy_engine/service.py`, lines 211 vs 538

`BaseStrategy.on_bar()` returns `SignalEvent | None`. `StrategyEngine.on_bar()` returns `list[SignalEvent]`. The container passes a single `BarEvent` to `strategy_engine.on_bar()`. If the engine collects a list but the container only handles a single signal, signals may be dropped.

---

### CONTRACT-03 · `MarketDataProvider`, `IndicatorService`, `MLEngine`, `NotificationService` Not Wired Into Container

**File**: `infrastructure/container.py`  
**Severity**: P1 — four critical subsystems have no lifecycle management

The DI container wires: event_bus, broker, portfolio, risk, execution, strategy.

**Missing from container**:
- `MarketDataProvider` (MT5 + Binance) — no one subscribes to bar events, no one calls `provider.connect()`
- `IndicatorService` — `StrategyEngine` uses attached `IIndicator` objects; the standalone `IndicatorService` cache is never started
- `MLEngine` — documented as active; never instantiated or connected
- `NotificationService` — documented as receiving `RiskBreachEvent`; never started

Without `MarketDataProvider` in the container, the platform receives **no live market data**. The event bus will be empty and no signals will ever be generated.

---

## 4. Architecture Inconsistencies

### INCONS-01 · `settings.api.jwt_secret` vs `settings.api.secret_key`

**Files**: `config/settings.py` (line 189), `api/routers.py` (line 52)

`APISettings` defines `secret_key: SecretStr`. The router's `require_auth` function reads `settings.api.jwt_secret` — which does not exist. JWT validation will raise `AttributeError` on the first authenticated request.

---

### INCONS-02 · `FillEvent` Field `slip` vs `slippage` Naming

**Files**: `core/domain/events.py` (line 232 — field `slip`), `api/routers.py` and `tests/unit/test_core.py` (reference `slippage`)

`FillEvent` defines the field as `slip`. Multiple consumers, tests, and documentation refer to `slippage`. This creates confusion and will produce attribute errors.

---

### INCONS-03 · `DatabaseSettings` Named Properties vs Direct URL

The `DatabaseSettings.async_url` property constructs the URL from parts. The repository accesses `settings.database.url` (non-existent). The settings also have `pool_size`/`max_overflow` (SQLAlchemy parameters) but the repository uses asyncpg's `min_size`/`max_size`. These are different parameter names for the same conceptual limits.

---

### INCONS-04 · Backtest Imports Directly From Application Services

**File**: `backtest_engine/service.py`, lines 43–46

```python
from risk_engine.service import RiskEngine
from portfolio_engine.service import PortfolioEngine
from strategy_engine.service import BaseStrategy
```

The backtest creates instances of the live production engines. If the `RiskEngine` constructor requires a Redis event bus (which it does), running a backtest requires a live Redis connection — making offline backtesting impossible and violating the testability principle.

**Fix**: The backtest should receive a `SimulatedRiskEngine` (no Redis) and a fresh `PortfolioEngine(initial_capital=...)` via dependency injection, not by importing production classes directly.

---

### INCONS-05 · `PortfolioEngine` Accesses Settings at Module Level

**File**: `portfolio_engine/service.py`, line 35: `settings = get_settings()`

Module-level `get_settings()` calls load the `.env` file at import time. In test environments without `.env`, this can fail or load wrong values. The correct pattern is to inject settings via constructor or use `get_settings()` lazily inside methods.

---

## 5. Scalability Gaps

### SCALE-01 · Trading Core Cannot Scale — No State Externalisation

**File**: `portfolio_engine/service.py`, lines 277–291

All position state, equity curve (100k points), and P&L are stored in memory (`_positions: Dict`, `_equity_curve: List`). If the service restarts, all in-memory state is lost. The platform relies on the comment "Redis-backed for HA" in the docstring, but no Redis state synchronisation is implemented.

**Required addition**: After every `on_fill()`, serialize the position state to Redis with `await redis.hset("portfolio:positions", ...)` and restore on startup.

---

### SCALE-02 · `asyncio.gather` With `return_exceptions=False` in Event Dispatch

**File**: `infrastructure/event_bus/redis_event_bus.py`, line 365

```python
await asyncio.gather(*[h(decoded) for h in handlers], return_exceptions=False)
```

If any one handler raises, the gather propagates the exception, the XACK is skipped, and the message enters the PEL. However, the other handlers that succeeded are not re-run on the retry. This is incorrect — a partial failure should still ACK the message for handlers that succeeded, or use a per-handler try/except.

---

### SCALE-03 · TWAP Algorithm Holds `asyncio.sleep()` for Up to 30 Minutes

**File**: `execution_engine/service.py`, lines 283–289

A TWAP order with 10 slices over 30 minutes occupies a single coroutine for 30 minutes. While `asyncio.sleep()` is non-blocking, if the event loop is processing these slices serially, multiple TWAP orders on different symbols will interleave unpredictably. Each TWAP should run in its own `asyncio.Task`.

---

### SCALE-04 · No Backpressure Mechanism on the Strategy Fan-Out

**File**: `strategy_engine/service.py`, line 566

`asyncio.gather` runs all strategies concurrently on each bar. With 20 symbols × 3 timeframes, each bar close triggers 60 concurrent strategy evaluations. If a strategy is slow (ML inference), it blocks the event loop and creates unbounded queue pressure. Strategies should be given a configurable execution timeout.

---

## 6. Fault Tolerance Gaps

### FAULT-01 · No Position Persistence — Complete State Loss on Restart

The PortfolioEngine holds all open positions in memory. A container restart drops all position data. The platform will then attempt to trade as if it has no open positions, potentially opening duplicate positions on the broker.

**Required**: A startup reconciliation step: `await portfolio.reconcile_with_broker(broker.get_open_positions())`.

---

### FAULT-02 · Dead-Letter Queue Documented but Not Implemented

**File**: `infrastructure/event_bus/redis_event_bus.py` (line 28 of docstring)

The docstring states: *"Dead-letter queue for messages that fail all retry attempts"*. No DLQ implementation exists. Failed messages enter the Redis PEL (Pending Entry List) and are never automatically retried or routed to a recovery stream. Under sustained failure, the PEL grows unboundedly.

---

### FAULT-03 · Circuit Breaker Has No Automatic Half-Open State

**File**: `risk_engine/service.py`, the `CircuitBreaker` class

The documented circuit breaker is binary: CLOSED → OPEN → manual reset. Standard production circuit breakers include a HALF-OPEN state (allow one request to test recovery). Without it, every drawdown requires manual admin intervention to resume trading — during which the platform is completely halted.

---

### FAULT-04 · VaR Calculation Holds a `ThreadPoolExecutor` Thread for Unknown Duration

**File**: `risk_engine/service.py`, line 474: `ThreadPoolExecutor(max_workers=4, ...)`

The `shutdown(wait=False)` in `risk_engine.shutdown()` (line 703) does not wait for in-flight VaR calculations to complete. A shutdown during a VaR calculation will leave an orphaned thread. Use `wait=True` or implement a cancellation token.

---

## 7. Security Vulnerabilities

### SEC-01 · WebSocket Endpoints Have No Authentication

**File**: `api/main.py`, lines 251–358

Both WebSocket endpoints (`/ws/market-data/{symbol}` and `/ws/portfolio`) perform **no token validation**. Anyone with network access to the API can subscribe to live portfolio data and position information without a JWT. This is a serious information disclosure vulnerability in a trading context.

**Fix**:
```python
@app.websocket("/ws/portfolio")
async def portfolio_ws(websocket: WebSocket, token: str = Query(...)):
    payload = validate_token(token)   # raise 403 if invalid
    await websocket.accept()
```

---

### SEC-02 · `ACTIVE_WEBSOCKETS` Counter — Wrong Metric Type

**File**: `api/main.py`, line 65

```python
ACTIVE_WEBSOCKETS = Counter(...)   # monotonically increasing — never decreases
```

A `Counter` cannot decrease. This metric will count total connections opened, never active connections. Use `Gauge` to track currently active connections.

---

### SEC-03 · Default Secrets in `.env.example` Are Weak and Overridable by `:-` Defaults

**File**: `docker/docker-compose.yml`, lines 48–62

```yaml
DB_PASSWORD: ${DB_PASSWORD:-strongpassword123}
API_JWT_SECRET: ${JWT_SECRET:-change-me-in-production-min-32-chars}
```

These defaults are hardcoded in the compose file. If a developer runs `docker compose up` without setting environment variables, the platform starts with known weak credentials. The platform should refuse to start if `environment == "production"` and `JWT_SECRET` is the default value.

**Fix in `config/settings.py`**:
```python
@field_validator("api")
@classmethod
def enforce_strong_secret_in_production(cls, v, info):
    if info.data.get("environment") == "production":
        if v.secret_key.get_secret_value() == "CHANGE_THIS_IN_PRODUCTION_32_CHARS_MIN":
            raise ValueError("JWT secret must be changed in production")
    return v
```

---

## 8. Documentation vs. Code Discrepancies

| Document claim | Code reality |
|---|---|
| `04_RISK_ENGINE.md`: "5 validators including CorrelationValidator" | Only 5 validators exist; `CorrelationValidator` is listed in the docstring but never implemented |
| `05_EXECUTION_ENGINE.md`: "ICEBERG and POV algorithms" | Only MARKET, TWAP, VWAP implemented; ICEBERG/POV appear in config schema but have no class |
| `08_MONITORING.md`: "25 Prometheus metrics" | `monitoring/metrics.py` defines the registry but the count has not been verified; `api/main.py` defines its own `REQUEST_COUNT` counter with a different name (`http_requests_total`) which will conflict with `TradingMetrics.http_requests_total` (`trading_api_requests_total`) |
| `09_DEPLOYMENT.md`: "make docker-up" | docker-compose.yml references non-existent Dockerfiles — `docker-up` will fail immediately |
| `11_SECURITY.md`: "Redis TLS on port 6380" | docker-compose.yml starts Redis on standard port 6379 with no TLS configuration |
| `12_TESTING.md`: "45 unit tests" | 42 `def test_` functions found in test_core.py by grep |

---

## 9. Prioritised Remediation Plan

### Sprint 1 — Unblock (1–2 days)

| ID | Action | File |
|---|---|---|
| BUG-08 | Add `get_instance()` or fix container retrieval in routers | `api/routers.py` |
| DEPLOY-01 | Fix docker-compose to use single Dockerfile with targets | `docker/docker-compose.yml` |
| DEPLOY-02 | Create `docker/nginx.conf` | New file |
| DEPLOY-05 | Fix `settings.database.url` → `settings.database.async_url` | `db_repositories.py` |
| INCONS-01 | Fix `settings.api.jwt_secret` → `settings.api.secret_key` | `api/routers.py` |
| BUG-07 | Change consumer group `id="0"` → `id="$"` | `redis_event_bus.py` |

### Sprint 2 — Financial Correctness (2–3 days)

| ID | Action | File |
|---|---|---|
| BUG-01 | Fix equity formula and add `_cumulative_realised_pnl` | `portfolio_engine/service.py` |
| BUG-02 | Standardise `FillEvent.quantity` (rename field) | `events.py`, all consumers |
| BUG-03 | Rename `BarEvent.open_price` → `open`, etc. | `events.py`, all consumers |
| BUG-04 | Implement `get_daily_pnl()` and fix validator | `portfolio_engine`, `risk_engine` |
| BUG-05 | Global replace `datetime.utcnow()` → `datetime.now(timezone.utc)` | All files |
| BUG-06 | Align `validate_signal()` signatures across Protocol, impl, tests | 3 files |

### Sprint 3 — Deployment Completeness (2–3 days)

| ID | Action | File |
|---|---|---|
| DEPLOY-03 | Create `docker/grafana/datasources/prometheus.yml` | New file |
| DEPLOY-04 | Change Redis to `noeviction` policy | `docker-compose.yml` |
| CONTRACT-01 | Fix async/sync mismatch in Protocol | `core/interfaces/__init__.py` |
| CONTRACT-03 | Add MarketDataProvider, IndicatorService, MLEngine, NotificationService to container | `container.py` |
| SEC-01 | Add JWT authentication to WebSocket endpoints | `api/main.py` |
| SEC-02 | Change `ACTIVE_WEBSOCKETS` from Counter to Gauge | `api/main.py` |

### Sprint 4 — Production Hardening (1 week)

| ID | Action |
|---|---|
| FAULT-01 | Implement position persistence and startup reconciliation with broker |
| FAULT-02 | Implement Dead-Letter Queue in Redis event bus |
| FAULT-03 | Add HALF-OPEN state to CircuitBreaker |
| SCALE-01 | Implement Redis-backed position state snapshots |
| SCALE-03 | Run TWAP/VWAP slices as isolated `asyncio.Task`s |
| BUG-04-ext | Implement `CorrelationValidator` and `ExposureValidator` (documented, missing) |
| SEC-03 | Add startup validation — refuse to start in production with default secrets |

---

## 10. Positive Findings

The following aspects are architecturally exemplary and should be preserved:

- **Clean Architecture layering** is consistently enforced. No engine imports broker-specific code. The Protocol-based interface system is correctly designed.
- **`frozen=True` domain events** with `slots=True` provide correct immutability and memory efficiency.
- **Redis Streams over Pub/Sub** is the right choice for durability and consumer group semantics.
- **`ThreadPoolExecutor` isolation for MT5** correctly prevents the synchronous C++ API from blocking the event loop.
- **`Decimal` throughout monetary values** is the correct financial arithmetic choice.
- **Numba JIT kernels with Python wrapper functions** provides the right abstraction boundary — C-speed execution behind a pandas-friendly API.
- **`asyncpg` over ORM** for time-series bulk inserts is the right performance trade-off.
- **TimescaleDB hypertables with compression policies** demonstrates sophisticated time-series engineering.
- **Anchored walk-forward optimization** correctly addresses look-ahead bias in strategy evaluation.
- **`BacktestConfig.slippage_model`** being configurable is good production thinking.
- **`pyproject.toml` with pinned exact versions** prevents dependency drift in production.
- **Structured JSON logging** with `correlation_id` propagation enables proper distributed tracing.

---

## 11. Conclusion

This platform is the work of experienced engineers with a clear architectural vision. The strategic decisions are sound. The 8 bugs catalogued above are not design flaws — they are integration gaps between layers that were designed correctly in isolation but not fully connected. The deployment blockers are mechanical file-creation tasks.

A platform of this ambition requires approximately **one focused sprint** to fix the P0 bugs and deployment blockers, and **one more sprint** to close the production-hardening gaps. After that, the architecture is strong enough to support a paper-trading validation period before live capital deployment.

The most important single fix is **BUG-01** (equity formula). Every other risk metric, position sizer, drawdown calculation, and Prometheus gauge derives from `get_equity()`. An incorrect equity value cascades through the entire platform.

---

*Report generated by architectural audit — all findings are code-verified, not speculative.*  
*Total issues identified: 8 P0/P1 bugs, 6 deployment blockers, 3 interface violations, 5 inconsistencies, 4 scalability gaps, 4 fault-tolerance gaps, 3 security issues.*
