"""
Monitoring Layer
================
Centralised observability infrastructure: Prometheus metrics, structured
logging, and health-check endpoints.

Design decisions
----------------
* All metrics are defined in a single registry module to prevent duplicate
  registrations (Prometheus raises if you register the same metric name twice).
* Metrics follow the naming convention: trading_{layer}_{metric}_{unit}.
* Structured logging uses Python's logging.LogRecord with extra fields that
  are picked up by Loki via the Promtail JSON formatter.
* Health checks are hierarchical: /health (lightweight) → /health/detailed
  (all subsystems) so load balancers can use the lightweight one.
* Latency histograms use pre-defined buckets tuned for trading latency:
  sub-millisecond (tick processing) to seconds (order round-trip).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Summary,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    # Stub classes so the rest of the code can import without error
    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def labels(self, **kw):
            return self

        def inc(self, *a):
            pass

        def dec(self, *a):
            pass

        def set(self, *a):
            pass

        def observe(self, *a):
            pass

        def time(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    Counter = Gauge = Histogram = Summary = _Stub

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric definitions — one module-level instance each
# ---------------------------------------------------------------------------

# Latency buckets: 0.1ms, 1ms, 5ms, 10ms, 50ms, 100ms, 500ms, 1s, 5s, 10s
LATENCY_BUCKETS = [0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]


class TradingMetrics:
    """Singleton metrics registry for the entire platform."""

    _instance: Optional["TradingMetrics"] = None

    def __new__(cls) -> "TradingMetrics":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_metrics()
        return cls._instance

    def _init_metrics(self) -> None:
        """Register all Prometheus metrics exactly once."""

        # ---- Market Data ----
        self.tick_received_total = Counter(
            "trading_market_data_ticks_total",
            "Total tick events received",
            ["symbol", "source"],
        )
        self.bar_received_total = Counter(
            "trading_market_data_bars_total",
            "Total bar events received",
            ["symbol", "timeframe"],
        )
        self.data_latency_seconds = Histogram(
            "trading_market_data_latency_seconds",
            "Tick processing latency",
            ["symbol"],
            buckets=LATENCY_BUCKETS,
        )

        # ---- Strategy Engine ----
        self.signals_generated_total = Counter(
            "trading_strategy_signals_total",
            "Total trading signals generated",
            ["strategy_id", "symbol", "direction"],
        )
        self.signal_generation_seconds = Histogram(
            "trading_strategy_signal_latency_seconds",
            "Signal generation latency per bar",
            ["strategy_id"],
            buckets=LATENCY_BUCKETS,
        )

        # ---- Risk Engine ----
        self.risk_validations_total = Counter(
            "trading_risk_validations_total",
            "Total risk validation attempts",
            ["result"],  # approved / rejected
        )
        self.risk_breaches_total = Counter(
            "trading_risk_breaches_total",
            "Total risk limit breaches by type",
            ["breach_type"],
        )
        self.current_drawdown_pct = Gauge(
            "trading_risk_drawdown_pct",
            "Current peak-to-trough drawdown percentage",
        )
        self.var_99_gauge = Gauge(
            "trading_risk_var_99_usd",
            "Current 99% Value-at-Risk in USD",
        )

        # ---- Execution Engine ----
        self.orders_submitted_total = Counter(
            "trading_execution_orders_total",
            "Total orders submitted to broker",
            ["symbol", "side", "algorithm"],
        )
        self.fills_received_total = Counter(
            "trading_execution_fills_total",
            "Total fills received",
            ["symbol", "side"],
        )
        self.order_round_trip_seconds = Histogram(
            "trading_execution_round_trip_seconds",
            "Order submission to fill latency",
            ["symbol", "algorithm"],
            buckets=LATENCY_BUCKETS,
        )
        self.slippage_bps = Histogram(
            "trading_execution_slippage_bps",
            "Execution slippage in basis points",
            ["symbol"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100],
        )

        # ---- Portfolio ----
        self.equity_usd = Gauge(
            "trading_portfolio_equity_usd",
            "Current portfolio equity in USD",
        )
        self.open_positions_count = Gauge(
            "trading_portfolio_open_positions",
            "Number of open positions",
        )
        self.realised_pnl_usd = Gauge(
            "trading_portfolio_realised_pnl_usd",
            "Cumulative realised P&L in USD",
        )
        self.unrealised_pnl_usd = Gauge(
            "trading_portfolio_unrealised_pnl_usd",
            "Current unrealised P&L in USD",
        )

        # ---- ML Engine ----
        self.ml_prediction_confidence = Histogram(
            "trading_ml_prediction_confidence",
            "ML model prediction confidence score",
            ["symbol", "model"],
            buckets=[0.1 * i for i in range(11)],
        )
        self.ml_regime_gauge = Gauge(
            "trading_ml_regime",
            "Current market regime (0=up, 1=down, 2=ranging, 3=volatile)",
            ["symbol"],
        )

        # ---- Event Bus ----
        self.events_published_total = Counter(
            "trading_eventbus_published_total",
            "Total events published to Redis Streams",
            ["channel"],
        )
        self.events_consumed_total = Counter(
            "trading_eventbus_consumed_total",
            "Total events consumed from Redis Streams",
            ["channel"],
        )
        self.event_processing_seconds = Histogram(
            "trading_eventbus_processing_seconds",
            "Event handler execution time",
            ["channel"],
            buckets=LATENCY_BUCKETS,
        )

        # ---- API ----
        self.http_requests_total = Counter(
            "trading_api_requests_total",
            "Total HTTP requests to the API",
            ["method", "endpoint", "status_code"],
        )
        self.http_request_latency_seconds = Histogram(
            "trading_api_request_latency_seconds",
            "HTTP request latency",
            ["method", "endpoint"],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
        )
        self.ws_connections_active = Gauge(
            "trading_api_ws_connections_active",
            "Number of active WebSocket connections",
        )

        # ---- System ----
        self.circuit_breaker_status = Gauge(
            "trading_circuit_breaker_open",
            "1 if circuit breaker is open (trading halted), 0 if closed",
        )
        self.platform_uptime_seconds = Gauge(
            "trading_platform_uptime_seconds",
            "Seconds since platform startup",
        )
        self._start_time = time.time()

    def update_uptime(self) -> None:
        self.platform_uptime_seconds.set(time.time() - self._start_time)


# Global singleton
metrics = TradingMetrics()


# ---------------------------------------------------------------------------
# Structured logging formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """
    Formats log records as JSON for ingestion by Loki via Promtail.

    Each record includes: timestamp, level, logger, message, and any
    extra fields set on the LogRecord (correlation_id, symbol, etc.).
    """

    RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        log_dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
            "module": record.module,
            "line": record.lineno,
        }
        # Attach extra fields (e.g. correlation_id, symbol)
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                log_dict[key] = value

        if record.exc_info:
            log_dict["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_dict, default=str)


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """
    Configure root logger with JSON formatter for production or
    human-readable formatter for development.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    # Avoid duplicate handlers on re-import
    if not root.handlers:
        root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ["urllib3", "asyncio", "aiohttp.access"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Health check subsystem
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    """Status of a single subsystem."""

    name: str
    healthy: bool
    latency_ms: float = 0.0
    message: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class HealthChecker:
    """
    Performs health checks on all platform subsystems.

    Used by /health/detailed API endpoint and Kubernetes readiness probes.
    """

    def __init__(self) -> None:
        self._checks: Dict[str, callable] = {}

    def register(self, name: str, check_fn) -> None:
        """Register an async health check function."""
        self._checks[name] = check_fn

    async def check_all(self) -> Dict[str, HealthStatus]:
        """Run all registered health checks concurrently."""
        results: Dict[str, HealthStatus] = {}
        tasks = {
            name: asyncio.create_task(self._run_check(name, fn))
            for name, fn in self._checks.items()
        }
        for name, task in tasks.items():
            results[name] = await task
        return results

    async def _run_check(self, name: str, fn) -> HealthStatus:
        t0 = time.perf_counter()
        try:
            healthy, message = await fn()
            latency_ms = (time.perf_counter() - t0) * 1000
            return HealthStatus(
                name=name,
                healthy=healthy,
                latency_ms=round(latency_ms, 2),
                message=message,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            return HealthStatus(
                name=name,
                healthy=False,
                latency_ms=round(latency_ms, 2),
                message=str(exc),
            )

    async def is_platform_healthy(self) -> bool:
        """Quick check: all subsystems healthy?"""
        results = await self.check_all()
        return all(s.healthy for s in results.values())


# Default global health checker
health_checker = HealthChecker()
