"""
Domain Protocol Interfaces — PEP 544 structural subtyping contracts.

Fixes applied (CONTRACT-01, BUG-06):
  - CONTRACT-01: get_positions(), get_equity(), get_realised_pnl() corrected
                 to sync (matching the PortfolioEngine implementation).
                 get_daily_pnl() added for DailyLossValidator (BUG-04).
  - BUG-06: validate_signal() signature standardised: only (signal) arg.

Design:
    All protocols are @runtime_checkable so isinstance() works in tests.
    Concrete implementations must NOT import from this module (dependency
    inversion: high-level modules define the interface; low-level implement).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from core.domain.events import (
    BarEvent,
    FillEvent,
    OrderEvent,
    SignalEvent,
    TickEvent,
)


@runtime_checkable
class IMarketDataProvider(Protocol):
    """Data provider abstraction — MT5, Binance, CSV, synthetic."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def subscribe(self, symbol: str, timeframe: str) -> None: ...
    async def unsubscribe(self, symbol: str, timeframe: str) -> None: ...

    async def get_historical_bars(
        self,
        symbol:     str,
        timeframe:  str,
        count:      int,
    ) -> list[BarEvent]: ...


@runtime_checkable
class IIndicatorEngine(Protocol):
    """Indicator computation abstraction."""

    async def compute(
        self,
        indicator: str,
        symbol:    str,
        timeframe: str,
        params:    dict[str, Any],
    ) -> dict[str, Any]: ...


@runtime_checkable
class IStrategy(Protocol):
    """Strategy contract — receives market data, emits signals."""

    @property
    def strategy_id(self) -> str: ...

    @property
    def symbols(self) -> list[str]: ...

    @property
    def timeframes(self) -> list[str]: ...

    async def on_bar(self, event: BarEvent) -> SignalEvent | None: ...
    async def on_tick(self, event: TickEvent) -> SignalEvent | None: ...


@runtime_checkable
class IRiskEngine(Protocol):
    """
    Risk engine contract.

    FIX BUG-06: validate_signal takes only (signal); portfolio is injected
    at construction time into the concrete implementation.
    """

    async def validate_signal(self, signal: SignalEvent) -> bool: ...

    async def calculate_var(
        self,
        confidence_level: float,
        horizon_days:     int,
    ) -> float: ...

    async def calculate_cvar(self, confidence_level: float) -> float: ...
    async def get_current_drawdown(self) -> float: ...


@runtime_checkable
class IExecutionEngine(Protocol):
    """Execution engine contract."""

    async def submit_order(self, order: OrderEvent) -> str: ...
    async def cancel_order(self, order_id: str) -> bool: ...
    async def get_open_orders(self) -> list[OrderEvent]: ...
    async def get_fills(self, from_ts: float) -> list[FillEvent]: ...


@runtime_checkable
class IPortfolioEngine(Protocol):
    """
    Portfolio engine contract.

    FIX CONTRACT-01: get_positions(), get_equity(), get_realised_pnl()
    are synchronous — they read from in-memory state with no I/O.
    Only on_fill() and on_tick() are async (they acquire a lock).
    get_daily_pnl() added for BUG-04 DailyLossValidator fix.
    """

    async def on_fill(self, fill: FillEvent) -> None: ...
    async def on_tick(self, tick: TickEvent) -> None: ...

    def get_positions(self) -> dict[str, Any]: ...
    def get_equity(self) -> Decimal: ...
    def get_realised_pnl(self) -> Decimal: ...
    def get_daily_pnl(self) -> Decimal: ...
    def get_unrealised_pnl(self) -> Decimal: ...
    def get_current_drawdown(self) -> float: ...
    def get_total_exposure(self) -> Decimal: ...
    def get_exposure_pct(self) -> float: ...
    def get_summary(self) -> dict[str, Any]: ...

    def calculate_position_size(
        self,
        symbol:    str,
        price:     float,
        daily_vol: float,
        lot_size:  float,
    ) -> Decimal: ...


@runtime_checkable
class IEventBus(Protocol):
    """Event bus contract — Redis Streams implementation."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, event: Any) -> str: ...
    async def publish_many(self, events: list[Any]) -> list[str]: ...
    def register_handler(self, channel: str, handler: Any) -> None: ...
    async def start_consuming(self) -> None: ...


@runtime_checkable
class INotificationService(Protocol):
    """Notification service contract."""

    async def send(self, subject: str, body: str, level: str = "INFO") -> bool: ...


@runtime_checkable
class IRepository(Protocol):
    """Generic repository contract."""

    async def save(self, entity: object) -> None: ...
    async def find_by_id(self, entity_id: str) -> object | None: ...
    async def find_all(self, limit: int = 100, offset: int = 0) -> list[object]: ...
    async def delete(self, entity_id: str) -> bool: ...
