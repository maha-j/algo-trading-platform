"""
pytest conftest.py
==================
Shared fixtures, markers, and asyncio configuration for the entire test suite.

All tests in this project are async-first.  pytest-asyncio is configured
in "auto" mode (set in pyproject.toml) so every coroutine test function
runs automatically without @pytest.mark.asyncio decoration (though we add
it for clarity in integration tests).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Asyncio event loop policy (required for Python 3.12 + pytest-asyncio)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default asyncio policy (uvloop optional for production)."""
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def gbm_bars_1000() -> pd.DataFrame:
    """
    Session-scoped 1000-bar GBM DataFrame.
    Expensive to compute — shared across all tests.
    """
    np.random.seed(42)
    n = 1000
    prices = [1.0850]
    for _ in range(n - 1):
        prices.append(prices[-1] * np.exp(np.random.normal(0, 0.001)))

    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [p * 0.9998 for p in prices],
            "high": [p * 1.0005 for p in prices],
            "low": [p * 0.9995 for p in prices],
            "close": prices,
            "volume": np.random.uniform(500, 3000, n),
        },
        index=idx,
    )


@pytest.fixture
def mock_settings():
    """
    A MagicMock settings object with sensible defaults for all tests.
    Prevents tests from loading real .env files.
    """
    s = MagicMock()
    s.risk.max_position_size_pct = 5.0
    s.risk.max_open_positions = 10
    s.risk.max_daily_loss_pct = 3.0
    s.risk.max_drawdown_pct = 15.0
    s.risk.var_confidence = 0.99
    s.execution.slippage_bps = 1.0
    s.execution.default_algorithm = "MARKET"
    s.notification.telegram_token = MagicMock()
    s.notification.telegram_token.get_secret_value.return_value = ""
    s.notification.telegram_chat_id = ""
    s.notification.smtp_host = ""
    s.notification.smtp_port = 587
    s.notification.smtp_user = ""
    s.notification.smtp_password = MagicMock()
    s.notification.smtp_password.get_secret_value.return_value = ""
    s.notification.smtp_from_email = "test@test.com"
    s.notification.alert_email_to = "alert@test.com"
    s.notification.webhook_url = ""
    s.api.jwt_secret = MagicMock()
    s.api.jwt_secret.get_secret_value.return_value = "test-secret-32-chars-minimum!!!!!"
    return s


@pytest.fixture
def portfolio_100k():
    """Fresh PortfolioEngine with $100,000 capital."""
    from portfolio_engine.service import PortfolioEngine

    return PortfolioEngine(initial_capital=100_000.0)


@pytest.fixture
def indicator_service():
    from indicator_engine.service import IndicatorService

    return IndicatorService()


# ---------------------------------------------------------------------------
# Pytest markers (suppress warnings for unregistered marks)
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: pure unit tests (no external deps)")
    config.addinivalue_line("markers", "integration: require Redis/Postgres")
    config.addinivalue_line("markers", "slow: slow tests (backtest, ML training)")
    config.addinivalue_line("markers", "benchmark: performance benchmarks")


# ---------------------------------------------------------------------------
# Performance benchmark fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def timer():
    """Simple wall-clock timer for non-pytest-benchmark performance checks."""
    import time

    class Timer:
        def __init__(self):
            self._start = 0.0

        def start(self):
            self._start = time.perf_counter()

        def elapsed(self) -> float:
            return time.perf_counter() - self._start

    return Timer()
