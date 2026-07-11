"""
Unit Tests — Core domain and engine components.

Fixes applied (all field names updated, validate_signal signature unified):
  - FillEvent: quantity (not quantity_filled), slippage (not slip)
  - BarEvent:  open/high/low/close (not open_price etc.)
  - validate_signal: takes only (signal) — portfolio injected at construction
  - datetime: timezone-aware throughout
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from core.domain.events import (
    BarEvent,
    EventType,
    FillEvent,
    OrderEvent,
    RiskBreachEvent,
    SignalEvent,
    TickEvent,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Test Factories
# ─────────────────────────────────────────────────────────────────────────────


def make_fill(
    symbol: str = "EURUSD",
    side: str = "BUY",
    qty: float = 1.0,
    price: float = 1.1000,
    commission: float = 7.0,
) -> FillEvent:
    """Factory for FillEvent test doubles with correct field names."""
    return FillEvent(
        source="test",
        order_id="ord-001",
        symbol=symbol,
        side=side,
        quantity=Decimal(str(qty)),  # FIX BUG-02: was quantity_filled
        fill_price=Decimal(str(price)),
        commission=Decimal(str(commission)),
        slippage=Decimal("0.00002"),  # FIX BUG-02: was slip
    )


def make_bar(
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    open_: float = 1.0850,
    high: float = 1.0860,
    low: float = 1.0840,
    close: float = 1.0855,
    volume: float = 1000.0,
    bar_index: int = 0,
) -> BarEvent:
    """Factory for BarEvent test doubles with correct field names."""
    return BarEvent(
        source="test",
        symbol=symbol,
        timeframe=timeframe,
        open=Decimal(str(open_)),  # FIX BUG-03: was open_price
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        bar_index=bar_index,
    )


def make_signal(
    symbol: str = "EURUSD",
    direction: str = "LONG",
    strength: float = 0.8,
    price: float = 1.0855,
) -> SignalEvent:
    return SignalEvent(
        source="test-strategy",
        symbol=symbol,
        strategy_id="test-strat",
        direction=direction,
        strength=strength,
        signal_price=Decimal(str(price)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Domain Events Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDomainEvents:
    """Verify immutability, field names, and computed properties."""

    def test_fill_event_fields_correct(self):
        """BUG-02: FillEvent.quantity and .slippage (not quantity_filled/.slip)."""
        fill = make_fill(qty=0.5, price=1.1010)
        assert fill.quantity == Decimal("0.5")
        assert fill.fill_price == Decimal("1.1010")
        assert fill.slippage == Decimal("0.00002")
        assert fill.event_type == EventType.ORDER_FILLED

    def test_fill_event_is_frozen(self):
        """Events must be immutable."""
        fill = make_fill()
        with pytest.raises((AttributeError, TypeError)):
            fill.quantity = Decimal("999")  # type: ignore

    def test_bar_event_fields_correct(self):
        """BUG-03: BarEvent.open/high/low/close (not open_price etc.)."""
        bar = make_bar(open_=1.0850, high=1.0870, low=1.0830, close=1.0855)
        assert bar.open == Decimal("1.0850")
        assert bar.high == Decimal("1.0870")
        assert bar.low == Decimal("1.0830")
        assert bar.close == Decimal("1.0855")

    def test_bar_event_computed_properties(self):
        bar = make_bar(open_=1.0850, high=1.0870, low=1.0830, close=1.0860)
        assert bar.body == Decimal("0.001")
        assert bar.bar_range == Decimal("0.004")

    def test_tick_event_mid_and_spread(self):
        tick = TickEvent(
            source="binance",
            symbol="BTCUSDT",
            bid=Decimal("50000"),
            ask=Decimal("50010"),
            volume=Decimal("1.5"),
        )
        assert tick.mid == Decimal("50005")
        assert tick.spread == Decimal("10")

    def test_base_event_timestamp_is_timezone_aware(self):
        """BUG-05: timestamp must be timezone-aware (not naive utcnow)."""
        fill = make_fill()
        assert fill.timestamp.tzinfo is not None, "timestamp must be tz-aware"

    def test_signal_event_defaults(self):
        sig = make_signal()
        assert sig.direction == "LONG"
        assert sig.event_type == EventType.SIGNAL
        assert sig.correlation_id != sig.event_id

    def test_order_event_generates_unique_ids(self):
        o1 = OrderEvent(source="test", symbol="EURUSD", side="BUY", quantity=Decimal("1"))
        o2 = OrderEvent(source="test", symbol="EURUSD", side="BUY", quantity=Decimal("1"))
        assert o1.order_id != o2.order_id

    def test_risk_breach_event_channel(self):
        assert RiskBreachEvent.channel() == "stream:risk"

    def test_bar_event_channel(self):
        assert BarEvent.channel() == "stream:bars"


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Engine Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPortfolioEngine:
    """Critical equity formula and P&L accumulation tests (BUG-01)."""

    def _make_engine(self, capital: float = 100_000.0):
        from portfolio_engine.service import PortfolioEngine

        return PortfolioEngine(initial_capital=capital)

    @pytest.mark.asyncio
    async def test_initial_equity_correct(self):
        """BUG-01: equity at t=0 must equal initial_capital, not 2×."""
        engine = self._make_engine(100_000.0)
        assert engine.get_equity() == Decimal("100000.00"), (
            f"Expected 100000.00 but got {engine.get_equity()} — "
            "possible double-counting of _initial_capital + _cash"
        )

    @pytest.mark.asyncio
    async def test_buy_fill_decreases_cash(self):
        engine = self._make_engine(100_000.0)
        fill = make_fill(side="BUY", qty=1.0, price=1000.0, commission=7.0)
        await engine.on_fill(fill)
        # cash should decrease by (price * qty + commission)
        expected_cash = Decimal("100000") - Decimal("1000") - Decimal("7")
        assert engine._cash == expected_cash

    @pytest.mark.asyncio
    async def test_buy_then_sell_equity_correct(self):
        """BUG-01: After opening and closing a flat position, equity == cash."""
        engine = self._make_engine(100_000.0)
        buy_fill = make_fill(side="BUY", qty=1.0, price=1000.0, commission=7.0)
        sel_fill = make_fill(side="SELL", qty=1.0, price=1010.0, commission=7.0)
        await engine.on_fill(buy_fill)
        await engine.on_fill(sel_fill)

        # Position is closed — no open positions
        assert len(engine.get_positions()) == 0
        # Equity = 100_000 - 7 + 1010 - 1000 - 7 = 99_996
        # equity = 100000 - 1092.5 (buy+comm) + 1088.5 (sell-comm) = 99996.00
        expected = Decimal("99996.00")
        assert engine.get_equity() == expected, f"Expected {expected} got {engine.get_equity()}"

    @pytest.mark.asyncio
    async def test_realised_pnl_not_lost_after_close(self):
        """BUG-01: Cumulative realised P&L must persist after position closes."""
        engine = self._make_engine(100_000.0)
        await engine.on_fill(make_fill(side="BUY", qty=1.0, price=1000.0, commission=0.0))
        await engine.on_fill(make_fill(side="SELL", qty=1.0, price=1100.0, commission=0.0))
        # Position is gone from dict — P&L must still be in accumulator
        # gross realised = (1100-1000)*1 = 100.00 (commission handled via cash)
        assert engine.get_realised_pnl() == Decimal("100.00"), (
            f"Expected 100.00 got {engine.get_realised_pnl()}"
        )

    @pytest.mark.asyncio
    async def test_daily_pnl_is_zero_before_any_fills(self):
        """BUG-04: daily_pnl starts at 0."""
        engine = self._make_engine()
        assert engine.get_daily_pnl() == Decimal("0")

    @pytest.mark.asyncio
    async def test_daily_pnl_accumulates_fills(self):
        """BUG-04: daily_pnl reflects today's fills."""
        engine = self._make_engine(100_000.0)
        await engine.on_fill(make_fill(side="BUY", qty=1.0, price=1000.0, commission=0.0))
        await engine.on_fill(make_fill(side="SELL", qty=1.0, price=1050.0, commission=0.0))
        daily = engine.get_daily_pnl()
        # gross: (1050-1000)*1 = 50.00
        assert daily == Decimal("50.00"), f"Expected 50.00 got {daily}"

    @pytest.mark.asyncio
    async def test_drawdown_zero_at_start(self):
        engine = self._make_engine()
        assert engine.get_current_drawdown() == 0.0

    @pytest.mark.asyncio
    async def test_on_tick_updates_unrealised(self):
        from portfolio_engine.service import PortfolioEngine

        engine = PortfolioEngine(initial_capital=100_000.0)
        # Open a long position
        await engine.on_fill(make_fill(side="BUY", qty=1.0, price=1000.0, commission=0.0))
        # Send a tick with higher price
        tick = TickEvent(
            source="test",
            symbol="EURUSD",
            bid=Decimal("1010"),
            ask=Decimal("1010"),
            volume=Decimal("0"),
        )
        await engine.on_tick(tick)
        pos = engine.get_positions().get("EURUSD")
        assert pos is not None
        assert pos.unrealised_pnl == Decimal("10.00")


# ─────────────────────────────────────────────────────────────────────────────
# Risk Engine Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRiskEngine:
    """Validate risk validators and circuit breaker behaviour."""

    def _make_portfolio(
        self,
        equity: float = 100_000.0,
        daily_pnl: float = 0.0,
    ):
        """Mock portfolio that returns correct sync values."""
        m = MagicMock()
        m.get_equity.return_value = Decimal(str(equity))
        m.get_positions.return_value = {}
        m.get_realised_pnl.return_value = Decimal("0")
        m.get_daily_pnl.return_value = Decimal(str(daily_pnl))
        m.get_current_drawdown.return_value = 0.0
        return m

    def _make_engine(self, portfolio=None):
        from risk_engine.service import RiskEngine

        if portfolio is None:
            portfolio = self._make_portfolio()
        return RiskEngine(
            portfolio=portfolio,
            event_bus=MagicMock(),
            returns_provider=None,
        )

    @pytest.mark.asyncio
    async def test_valid_signal_approved(self):
        engine = self._make_engine()
        signal = make_signal()
        result = await engine.validate_signal(signal)
        assert result is True

    @pytest.mark.asyncio
    async def test_daily_loss_breach_rejected(self):
        """BUG-04: DailyLossValidator uses get_daily_pnl (daily scope)."""
        from config.settings import get_settings

        settings = get_settings()
        # Simulate -5% daily loss (beyond 3% limit)
        daily_loss = -float(100_000 * settings.risk.max_daily_loss_pct * 1.5)
        portfolio = self._make_portfolio(equity=100_000.0, daily_pnl=daily_loss)
        engine = self._make_engine(portfolio)
        signal = make_signal()
        result = await engine.validate_signal(signal)
        assert result is False, "Should be rejected due to daily loss breach"

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_after_trip(self):
        engine = self._make_engine()
        engine._circuit_breaker.trip(reason="test")
        signal = make_signal()
        result = await engine.validate_signal(signal)
        assert result is False, "Circuit breaker is OPEN — should block"

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset(self):
        engine = self._make_engine()
        engine._circuit_breaker.trip(reason="test")
        engine.reset_circuit_breaker(operator_id="test-operator")
        assert engine.circuit_breaker_state == "CLOSED"

    def test_circuit_breaker_half_open_after_timer(self):
        """FAULT-03: Circuit breaker transitions to HALF_OPEN automatically."""

        from risk_engine.service import CircuitBreaker

        cb = CircuitBreaker(half_open_after_seconds=0.001)
        cb.trip(reason="test")
        assert cb._state == "OPEN"
        import time

        time.sleep(0.01)
        # Accessing .state triggers auto-transition
        assert cb.state == "HALF_OPEN"

    def test_circuit_breaker_half_open_allows_one_probe(self):
        import time

        from risk_engine.service import CircuitBreaker

        cb = CircuitBreaker(half_open_after_seconds=0.001)
        cb.trip(reason="test")
        time.sleep(0.01)
        _ = cb.state  # trigger HALF_OPEN transition
        assert cb.can_probe() is True
        assert cb.can_probe() is False  # only one probe allowed

    @pytest.mark.asyncio
    async def test_var_calculation_returns_float(self):
        engine = self._make_engine()

        async def _async_returns():
            return pd.Series(np.random.normal(0, 0.01, 252))

        engine._returns_provider = _async_returns
        var = await engine.calculate_var()
        assert isinstance(var, float)
        assert 0.0 <= var <= 0.10


# ─────────────────────────────────────────────────────────────────────────────
# Event Bus Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRedisEventBus:
    """Test event bus serialisation and consumer group logic."""

    def test_serialise_fill_event(self):
        """Verify _serialise_event handles Decimal and datetime correctly."""
        from infrastructure.event_bus.redis_event_bus import _serialise_event

        fill = make_fill()
        serialised = _serialise_event(fill)
        assert isinstance(serialised, dict)
        assert all(isinstance(v, str) for v in serialised.values())
        # Decimal serialises to its canonical string form
        assert serialised["quantity"] in ("1", "1.0")  # Decimal("1") = "1"
        assert serialised["fill_price"] == "1.1"

    def test_serialise_bar_event(self):
        from infrastructure.event_bus.redis_event_bus import _serialise_event

        bar = make_bar()
        s = _serialise_event(bar)
        assert s["symbol"] == "EURUSD"
        assert s["open"] == "1.085"

    def test_consumer_group_uses_dollar_id(self):
        """BUG-07: Consumer group must start from '$' not '0'."""
        import inspect

        from infrastructure.event_bus import redis_event_bus as eb_module

        source = inspect.getsource(eb_module)
        assert 'id="$"' in source or "id='$'" in source, (
            "Consumer group id must be '$' to avoid history replay on restart"
        )

    def test_dlq_stream_defined(self):
        """FAULT-02: DLQ stream must be defined."""
        from infrastructure.event_bus.redis_event_bus import DLQ_STREAM

        assert DLQ_STREAM == "stream:dlq"


# ─────────────────────────────────────────────────────────────────────────────
# Settings Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSettings:
    """Verify configuration validation."""

    def test_settings_load(self):
        from config.settings import get_settings

        s = get_settings()
        assert s.environment in ("development", "staging", "production")

    def test_database_has_asyncpg_dsn(self):
        from config.settings import get_settings

        dsn = get_settings().database.asyncpg_dsn
        assert dsn.startswith("postgresql://")

    def test_api_secret_key_field_exists(self):
        """INCONS-01: Field must be secret_key (not jwt_secret)."""
        from config.settings import get_settings

        s = get_settings()
        assert hasattr(s.api, "secret_key"), "APISettings must have secret_key field"
        assert not hasattr(s.api, "jwt_secret"), "jwt_secret should not exist (use secret_key)"

    def test_risk_settings_valid_ranges(self):
        from config.settings import get_settings

        r = get_settings().risk
        assert 0 < r.max_position_size_pct <= 0.25
        assert 0 < r.max_daily_loss_pct <= 0.15
        assert 0 < r.max_drawdown_pct <= 0.50
        assert 0.90 <= r.var_confidence_level <= 0.9999


# ─────────────────────────────────────────────────────────────────────────────
# Position Sizer Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPositionSizer:
    def _sizer(self):
        from portfolio_engine.service import PositionSizer

        return PositionSizer(target_vol_pct=0.01, max_position_pct=0.05, kelly_fraction=0.5)

    def test_vol_target_returns_nonzero(self):
        s = self._sizer()
        size = s.vol_target_size(
            equity=Decimal("100000"),
            price=Decimal("1000"),
            daily_vol=0.02,
        )
        assert size > 0

    def test_zero_vol_returns_zero(self):
        s = self._sizer()
        assert s.vol_target_size(Decimal("100000"), Decimal("1000"), 0.0) == Decimal("0")

    def test_zero_equity_returns_zero(self):
        s = self._sizer()
        assert s.vol_target_size(Decimal("0"), Decimal("1000"), 0.02) == Decimal("0")

    def test_kelly_with_positive_edge(self):
        s = self._sizer()
        size = s.kelly_size(
            equity=Decimal("100000"),
            price=Decimal("100"),
            win_rate=0.60,
            avg_win=1.5,
            avg_loss=1.0,
        )
        assert size > 0

    def test_kelly_with_negative_edge_returns_zero(self):
        s = self._sizer()
        size = s.kelly_size(
            equity=Decimal("100000"),
            price=Decimal("100"),
            win_rate=0.30,
            avg_win=0.5,
            avg_loss=1.0,
        )
        assert size == Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# Interface Compliance Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestInterfaceCompliance:
    """Verify concrete implementations satisfy Protocol contracts."""

    def test_portfolio_engine_satisfies_protocol(self):
        from core.interfaces import IPortfolioEngine
        from portfolio_engine.service import PortfolioEngine

        engine = PortfolioEngine(initial_capital=100_000.0)
        assert isinstance(engine, IPortfolioEngine), (
            "PortfolioEngine must satisfy IPortfolioEngine Protocol"
        )

    def test_portfolio_engine_get_equity_is_sync(self):
        """CONTRACT-01: get_equity must be sync (not a coroutine)."""
        from portfolio_engine.service import PortfolioEngine

        engine = PortfolioEngine(initial_capital=100_000.0)
        result = engine.get_equity()
        assert not asyncio.iscoroutine(result), "get_equity must be sync"
        assert isinstance(result, Decimal)

    def test_portfolio_engine_get_positions_is_sync(self):
        from portfolio_engine.service import PortfolioEngine

        engine = PortfolioEngine(initial_capital=100_000.0)
        result = engine.get_positions()
        assert not asyncio.iscoroutine(result)
        assert isinstance(result, dict)

    def test_risk_engine_satisfies_protocol(self):
        from core.interfaces import IRiskEngine
        from risk_engine.service import RiskEngine

        engine = RiskEngine(
            portfolio=MagicMock(),
            event_bus=MagicMock(),
        )
        assert isinstance(engine, IRiskEngine)
