"""
Domain Events — Immutable value objects transmitted on the event bus.

Fixes applied (BUG-02, BUG-03, BUG-05):
  - BUG-02: FillEvent.quantity_filled renamed to FillEvent.quantity for
            consistency with OrderEvent and all consumers.
  - BUG-03: BarEvent.open_price/high_price/low_price/close_price renamed to
            open/high/low/close — standard convention in all financial libs.
  - BUG-05: All datetime.utcnow() replaced with datetime.now(timezone.utc)
            to produce timezone-aware datetimes compatible with TimescaleDB
            TIMESTAMPTZ and Python 3.12 (utcnow deprecated).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    """Canonical event type identifiers for Redis Stream routing."""

    TICK = "TICK"
    BAR = "BAR"
    INDICATOR = "INDICATOR"
    SIGNAL = "SIGNAL"
    ORDER_NEW = "ORDER_NEW"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    PORTFOLIO_UPDATE = "PORTFOLIO_UPDATE"
    RISK_BREACH = "RISK_BREACH"
    SYSTEM_ALERT = "SYSTEM_ALERT"


@dataclass(frozen=True, slots=True)
class BaseEvent:
    """
    Immutable base class for all domain events.

    Attributes:
        event_type:     Discriminator field for deserialisation routing.
        source:         Originating service/component name.
        timestamp:      UTC creation time with microsecond precision.
        event_id:       Unique UUID for idempotency and deduplication.
        correlation_id: Trace ID propagated across service boundaries.
    """

    event_type: EventType
    source: str
    # FIX BUG-05: datetime.now(timezone.utc) — timezone-aware, not deprecated
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def channel(cls) -> str:
        """Redis channel name for this event type. Override in subclasses."""
        raise NotImplementedError(f"{cls.__name__} must define channel()")


@dataclass(frozen=True, slots=True)
class TickEvent(BaseEvent):
    """
    Real-time best-bid/ask from a data provider.

    Attributes:
        symbol:   Normalized instrument identifier (e.g. 'EURUSD', 'BTCUSDT').
        bid:      Best bid price.
        ask:      Best ask price.
        volume:   Last traded volume (0 for FX if unavailable).
        provider: Data source identifier.
    """

    event_type: EventType = field(default=EventType.TICK, init=False)
    symbol: str = ""
    bid: Decimal = Decimal("0")
    ask: Decimal = Decimal("0")
    volume: Decimal = Decimal("0")
    provider: str = ""

    @property
    def mid(self) -> Decimal:
        """Mid-price: arithmetic mean of bid and ask."""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        """Quoted spread in price units."""
        return self.ask - self.bid

    @classmethod
    def channel(cls) -> str:
        return "stream:ticks"


@dataclass(frozen=True, slots=True)
class BarEvent(BaseEvent):
    """
    Aggregated OHLCV bar — the primary input to most strategies.

    FIX BUG-03: Fields renamed from open_price/high_price/low_price/close_price
    to open/high/low/close — standard convention in pandas, TA-Lib, backtrader,
    and all financial libraries. Eliminates AttributeError in backtest engine
    (next_bar.open) and test fixtures.

    Attributes:
        symbol:    Instrument identifier.
        timeframe: Bar duration string (e.g. 'M1', 'H1', 'D1').
        open:      Opening price for the period.
        high:      Highest traded price.
        low:       Lowest traded price.
        close:     Closing (last) price.
        volume:    Traded volume.
        bar_index: Monotonic integer index within stream.
        is_closed: False if the bar is still forming.
    """

    event_type: EventType = field(default=EventType.BAR, init=False)
    symbol: str = ""
    timeframe: str = ""
    open: Decimal = Decimal("0")
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    close: Decimal = Decimal("0")
    volume: Decimal = Decimal("0")
    bar_index: int = 0
    is_closed: bool = True

    @property
    def body(self) -> Decimal:
        """Absolute candle body size."""
        return abs(self.close - self.open)

    @property
    def bar_range(self) -> Decimal:
        """High-low range."""
        return self.high - self.low

    @classmethod
    def channel(cls) -> str:
        return "stream:bars"


@dataclass(frozen=True, slots=True)
class SignalEvent(BaseEvent):
    """
    Trading signal emitted by a Strategy.

    Attributes:
        symbol:       Target instrument.
        strategy_id:  Strategy that generated the signal.
        direction:    'LONG', 'SHORT', or 'FLAT' (exit).
        strength:     Normalised conviction in [0, 1].
        signal_price: Price at signal generation time.
        timeframe:    Source timeframe that triggered the signal.
        metadata:     Arbitrary dict for strategy-specific context.
    """

    event_type: EventType = field(default=EventType.SIGNAL, init=False)
    symbol: str = ""
    strategy_id: str = ""
    direction: str = ""
    strength: float = 0.0
    signal_price: Decimal = Decimal("0")
    timeframe: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def channel(cls) -> str:
        return "stream:signals"


@dataclass(frozen=True, slots=True)
class OrderEvent(BaseEvent):
    """
    Instruction to the Execution Engine to place/modify/cancel an order.

    Attributes:
        order_id:     Unique order identifier (platform-generated).
        symbol:       Target instrument.
        order_type:   'MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT'.
        side:         'BUY' or 'SELL'.
        quantity:     Lot size or number of units.
        limit_price:  Required for LIMIT orders.
        stop_price:   Required for STOP orders.
        strategy_id:  Source strategy for attribution.
        algorithm:    Execution algo ('MARKET', 'TWAP', 'VWAP').
        risk_approved: True if Risk Engine has validated.
    """

    event_type: EventType = field(default=EventType.ORDER_NEW, init=False)
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = ""
    order_type: str = "MARKET"
    side: str = ""
    quantity: Decimal = Decimal("0")
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    strategy_id: str = ""
    algorithm: str = "MARKET"
    risk_approved: bool = False

    @classmethod
    def channel(cls) -> str:
        return "stream:orders"


@dataclass(frozen=True, slots=True)
class FillEvent(BaseEvent):
    """
    Execution confirmation from the broker/exchange.

    FIX BUG-02: Renamed quantity_filled → quantity to match OrderEvent.quantity
    and all consumer code in portfolio_engine, execution_engine, tests, etc.

    Attributes:
        order_id:   ID of the filled order.
        symbol:     Executed instrument.
        side:       'BUY' or 'SELL'.
        quantity:   Actual filled quantity (may be partial).
        fill_price: Volume-weighted average fill price.
        commission: Brokerage commission in account currency.
        slippage:   Signed slippage = fill_price minus expected price.
    """

    event_type: EventType = field(default=EventType.ORDER_FILLED, init=False)
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    quantity: Decimal = Decimal("0")
    fill_price: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")

    @classmethod
    def channel(cls) -> str:
        return "stream:fills"


@dataclass(frozen=True, slots=True)
class RiskBreachEvent(BaseEvent):
    """
    Emitted when any risk limit is breached — triggers circuit-breaker logic.

    Attributes:
        breach_type:   Type of breach (e.g. 'DrawdownValidator', 'VaRValidator').
        current_value: Observed metric value at breach time.
        limit_value:   Configured limit that was exceeded.
        action:        Automated action taken ('BLOCK_NEW', 'FLATTEN', 'ALERT_ONLY').
    """

    event_type: EventType = field(default=EventType.RISK_BREACH, init=False)
    breach_type: str = ""
    current_value: float = 0.0
    limit_value: float = 0.0
    action: str = "ALERT_ONLY"

    @classmethod
    def channel(cls) -> str:
        return "stream:risk"
