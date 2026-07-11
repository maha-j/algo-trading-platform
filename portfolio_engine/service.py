"""
Portfolio Engine
================
Tracks open positions, realised/unrealised P&L, equity curve, and position
sizing across all asset classes.

Fixes applied (BUG-01, BUG-02, BUG-04, CONTRACT-01):
  - BUG-01: Equity formula fixed. _cash starts at initial_capital and is
            adjusted by fills. _cumulative_realised_pnl accumulates closed
            P&L so it is never lost when positions are deleted from the dict.
            get_equity() = _cash + unrealised_total (no double-counting).
  - BUG-02: FillEvent.quantity used (field unified to 'quantity').
  - BUG-04: get_daily_pnl() added for DailyLossValidator.
  - CONTRACT-01: Protocol-facing methods kept sync (Protocol will be updated).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Dict, List

import numpy as np

from config.settings import get_settings
from core.domain.events import FillEvent, TickEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class AssetClass(str, Enum):
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    STOCKS = "STOCKS"
    FUTURES = "FUTURES"


@dataclass
class Position:
    """
    Represents a live position in a single instrument.

    net_qty  > 0  → long
    net_qty  < 0  → short
    net_qty == 0  → flat (deleted from active dict after close)
    """

    symbol: str
    asset_class: AssetClass

    net_qty: Decimal = Decimal("0")
    avg_entry_price: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")
    unrealised_pnl: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")

    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    current_price: Decimal = Decimal("0")

    @property
    def market_value(self) -> Decimal:
        return (self.net_qty * self.current_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def total_pnl(self) -> Decimal:
        return self.realised_pnl + self.unrealised_pnl

    @property
    def is_long(self) -> bool:
        return self.net_qty > 0

    @property
    def is_flat(self) -> bool:
        return self.net_qty == 0

    def update_unrealised(self, current_price: Decimal) -> None:
        """Recompute unrealised P&L given a new market price."""
        self.current_price = current_price
        if self.net_qty == 0 or self.avg_entry_price == 0:
            self.unrealised_pnl = Decimal("0")
            return
        price_diff = current_price - self.avg_entry_price
        self.unrealised_pnl = (price_diff * self.net_qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        self.last_updated = datetime.now(timezone.utc)

    def apply_fill(self, fill: FillEvent) -> Decimal:
        """
        Update position state after a fill (FIFO/VWAP cost basis).

        Returns the realised P&L for this specific fill so the caller
        can accumulate it in _cumulative_realised_pnl.
        """
        fill_qty = Decimal(str(fill.quantity))
        fill_price = Decimal(str(fill.fill_price))
        commission = Decimal(str(fill.commission))

        self.commission_paid += commission
        this_realised = Decimal("0")
        # NOTE: commission deducted from cash in on_fill() — NOT from this_realised

        if self.net_qty == 0:
            # Opening a new position
            self.avg_entry_price = fill_price
            self.net_qty = fill_qty if fill.side == "BUY" else -fill_qty
        else:
            fill_signed = fill_qty if fill.side == "BUY" else -fill_qty
            new_qty = self.net_qty + fill_signed

            if self.net_qty > 0 and fill_signed < 0:
                # Partial or full close of long
                closed = min(abs(fill_signed), self.net_qty)
                this_realised = (fill_price - self.avg_entry_price) * closed
                self.realised_pnl += this_realised.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif self.net_qty < 0 and fill_signed > 0:
                # Partial or full close of short
                closed = min(fill_signed, abs(self.net_qty))
                this_realised = (self.avg_entry_price - fill_price) * closed
                self.realised_pnl += this_realised.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif (self.net_qty > 0 and fill_signed > 0) or (self.net_qty < 0 and fill_signed < 0):
                # Adding to existing position — recalculate VWAP entry
                total_cost = self.avg_entry_price * abs(self.net_qty) + fill_price * fill_qty
                self.avg_entry_price = total_cost / (abs(self.net_qty) + fill_qty)

            self.net_qty = new_qty

        self.last_updated = datetime.now(timezone.utc)
        logger.debug(
            "Position updated | %s | qty=%s avg=%.5f realised=%.2f",
            self.symbol,
            self.net_qty,
            float(self.avg_entry_price),
            float(self.realised_pnl),
        )
        # Return GROSS realised P&L — commission is handled separately in on_fill() cash flows
        return this_realised


# ---------------------------------------------------------------------------
# Position sizing models
# ---------------------------------------------------------------------------


class PositionSizer:
    """
    Computes the recommended position size in base-currency units.

    Two methods blended:
      1. Volatility targeting: size = (equity × target_vol_pct) / (σ × price)
      2. Half-Kelly: kelly_fraction capped at 0.5 × full Kelly
    Final size = min(vol_target_size, half_kelly_size) capped at max_position_pct.
    """

    def __init__(
        self,
        target_vol_pct: float = 0.01,
        max_position_pct: float = 0.05,
        kelly_fraction: float = 0.5,
    ) -> None:
        self.target_vol_pct = target_vol_pct
        self.max_position_pct = max_position_pct
        self.kelly_fraction = kelly_fraction

    def vol_target_size(
        self,
        equity: Decimal,
        price: Decimal,
        daily_vol: float,
        lot_size: Decimal = Decimal("1"),
    ) -> Decimal:
        """Return position size in units (rounded to lot_size)."""
        if daily_vol <= 0 or price <= 0 or equity <= 0:
            return Decimal("0")

        price_vol = float(price) * daily_vol
        target_risk = float(equity) * self.target_vol_pct
        raw_units = target_risk / price_vol if price_vol > 0 else 0
        max_units = float(equity) * self.max_position_pct / float(price)

        units = min(raw_units, max_units)
        lots = Decimal(str(units)) / lot_size
        lots = lots.to_integral_value(rounding=ROUND_HALF_UP)
        return max(Decimal("0"), lots * lot_size)

    def kelly_size(
        self,
        equity: Decimal,
        price: Decimal,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        lot_size: Decimal = Decimal("1"),
    ) -> Decimal:
        """Half-Kelly position size. Returns 0 if edge is negative."""
        if avg_loss <= 0:
            return Decimal("0")
        R = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / R
        if kelly <= 0:
            return Decimal("0")
        half_kelly = kelly * self.kelly_fraction
        raw_units = float(equity) * half_kelly / float(price)
        max_units = float(equity) * self.max_position_pct / float(price)
        units = min(raw_units, max_units)
        lots = Decimal(str(units)) / lot_size
        lots = lots.to_integral_value(rounding=ROUND_HALF_UP)
        return max(Decimal("0"), lots * lot_size)

    def recommended_size(
        self,
        equity: Decimal,
        price: Decimal,
        daily_vol: float,
        win_rate: float = 0.55,
        avg_win: float = 1.5,
        avg_loss: float = 1.0,
        lot_size: Decimal = Decimal("1"),
    ) -> Decimal:
        """Blended size: min(vol_target, half_kelly)."""
        vs = self.vol_target_size(equity, price, daily_vol, lot_size)
        ks = self.kelly_size(equity, price, win_rate, avg_win, avg_loss, lot_size)
        if ks == 0:
            return vs
        return min(vs, ks)


# ---------------------------------------------------------------------------
# Portfolio Engine
# ---------------------------------------------------------------------------


class PortfolioEngine:
    """
    Tracks live positions, equity, and P&L.

    FIX BUG-01 — Equity formula corrected:
      BEFORE (wrong): _cash + _initial_capital + unrealised + realised_from_open_pos
      AFTER  (correct): _cash + unrealised_total
      Where:
        _cash is seeded with initial_capital and adjusted on every fill.
        Closed P&L accumulates in _cumulative_realised_pnl (never lost).

      get_equity() = _cash + sum(unrealised of open positions)

    Thread safety:
      All state mutations happen inside asyncio coroutines on a single event
      loop — no locks needed for the in-memory dict.  The asyncio.Lock guards
      _cash and _positions to prevent concurrent on_fill() calls.
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        base_currency: str = "USD",
        event_bus=None,
    ) -> None:
        settings = get_settings()

        self._positions: Dict[str, Position] = {}
        self._equity_curve: List[tuple] = []
        self._initial_capital: Decimal = Decimal(str(initial_capital))
        self._cash: Decimal = Decimal(str(initial_capital))
        self._cumulative_realised_pnl: Decimal = Decimal("0")
        self._cumulative_commission: Decimal = Decimal("0")
        self._base_currency: str = base_currency
        self._event_bus = event_bus
        self._lock = asyncio.Lock()

        self._sizer = PositionSizer(
            target_vol_pct=settings.risk.max_position_size_pct / 2,
            max_position_pct=settings.risk.max_position_size_pct,
        )
        self._peak_equity = self._initial_capital

        # Track daily fills for DailyLossValidator (BUG-04)
        self._daily_fills: List[tuple] = []  # (timestamp, net_pnl)

        logger.info(
            "PortfolioEngine initialised | capital=%.2f %s",
            initial_capital,
            base_currency,
        )

    # ------------------------------------------------------------------
    # Public API — event handlers
    # ------------------------------------------------------------------

    async def on_fill(self, fill: FillEvent) -> None:
        """Process an execution fill and update portfolio state."""
        async with self._lock:
            symbol = fill.symbol
            if symbol not in self._positions:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    asset_class=self._classify_asset(symbol),
                )

            pos = self._positions[symbol]

            # apply_fill returns the net realised P&L for this fill
            net_realised = pos.apply_fill(fill)

            # FIX BUG-01: accumulate closed P&L before deleting position
            self._cumulative_realised_pnl += net_realised.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            self._cumulative_commission += Decimal(str(fill.commission))

            # Adjust cash — buys decrease cash, sells increase cash
            fill_value = Decimal(str(fill.fill_price)) * Decimal(str(fill.quantity))
            commission = Decimal(str(fill.commission))
            if fill.side == "BUY":
                self._cash -= fill_value + commission
            else:
                self._cash += fill_value - commission

            # Remove flat positions from dict
            if pos.is_flat:
                del self._positions[symbol]
                logger.info("Position closed | %s | net_pnl=%.2f", symbol, float(net_realised))

            # Track for daily P&L (BUG-04)
            self._daily_fills.append((datetime.now(timezone.utc), net_realised))

            self._record_equity_snapshot()
            logger.info(
                "Fill processed | %s %s %.4f @ %.5f | equity=%.2f",
                fill.side,
                fill.symbol,
                float(fill.quantity),
                float(fill.fill_price),
                float(self.get_equity()),
            )

    async def on_tick(self, tick: TickEvent) -> None:
        """Update unrealised P&L for a symbol when a new tick arrives."""
        if tick.symbol not in self._positions:
            return
        mid = (tick.bid + tick.ask) / 2
        async with self._lock:
            if tick.symbol in self._positions:
                self._positions[tick.symbol].update_unrealised(mid)

    # ------------------------------------------------------------------
    # Public API — state readers (sync — fast, in-memory only)
    # ------------------------------------------------------------------

    def get_positions(self) -> Dict[str, Position]:
        """Return a snapshot of all open positions (dict copy)."""
        return dict(self._positions)

    def get_equity(self) -> Decimal:
        """
        FIX BUG-01 — Total equity = cash balance + open unrealised P&L.

        _cash is seeded with initial_capital and adjusted on every fill.
        It already embeds all realised P&L implicitly through fill accounting.
        Unrealised P&L from open positions is added on top.
        """
        unrealised_total = sum(p.unrealised_pnl for p in self._positions.values())
        return (self._cash + unrealised_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_realised_pnl(self) -> Decimal:
        """Cumulative net realised P&L (all closed trades, all time)."""
        return self._cumulative_realised_pnl

    def get_daily_pnl(self) -> Decimal:
        """
        FIX BUG-04 — Net P&L for today only (UTC calendar day).
        Used by DailyLossValidator to correctly implement daily loss limits.
        """
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        daily = Decimal("0")
        for ts, pnl in self._daily_fills:
            if ts >= today_start:
                daily += pnl
        return daily

    def get_unrealised_pnl(self) -> Decimal:
        """Sum of unrealised P&L across all open positions."""
        return sum(p.unrealised_pnl for p in self._positions.values())

    def get_current_drawdown(self) -> float:
        """Peak-to-trough drawdown as a fraction [0.0, 1.0]."""
        equity = self.get_equity()
        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity == 0:
            return 0.0
        return max(0.0, float((self._peak_equity - equity) / self._peak_equity))

    def get_total_exposure(self) -> Decimal:
        """Sum of absolute market values across all open positions."""
        return sum(abs(p.market_value) for p in self._positions.values())

    def get_exposure_pct(self) -> float:
        """Gross exposure as percentage of current equity."""
        equity = self.get_equity()
        if equity == 0:
            return 0.0
        return float(self.get_total_exposure() / equity * 100)

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        daily_vol: float,
        lot_size: float = 1.0,
    ) -> Decimal:
        """Recommended position size (units) for a new trade."""
        return self._sizer.recommended_size(
            equity=self.get_equity(),
            price=Decimal(str(price)),
            daily_vol=daily_vol,
            lot_size=Decimal(str(lot_size)),
        )

    def get_returns_series(self) -> "np.ndarray":
        """Percentage returns from the equity curve for VaR/CVaR."""
        if len(self._equity_curve) < 2:
            return np.array([])
        values = np.array([float(v) for _, v in self._equity_curve])
        returns = np.diff(values) / values[:-1]
        return returns

    def get_summary(self) -> dict:
        """Serialisable summary for API endpoints and dashboard."""
        positions = [
            {
                "symbol": p.symbol,
                "asset_class": p.asset_class.value,
                "net_qty": float(p.net_qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealised_pnl": float(p.unrealised_pnl),
                "realised_pnl": float(p.realised_pnl),
                "market_value": float(p.market_value),
            }
            for p in self._positions.values()
        ]
        return {
            "equity": float(self.get_equity()),
            "initial_capital": float(self._initial_capital),
            "cash": float(self._cash),
            "realised_pnl": float(self.get_realised_pnl()),
            "unrealised_pnl": float(self.get_unrealised_pnl()),
            "total_pnl": float(self.get_realised_pnl() + self.get_unrealised_pnl()),
            "drawdown_pct": round(self.get_current_drawdown() * 100, 2),
            "exposure_pct": round(self.get_exposure_pct(), 2),
            "open_positions": len(self._positions),
            "positions": positions,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_equity_snapshot(self) -> None:
        equity = self.get_equity()
        ts = datetime.now(timezone.utc)
        self._equity_curve.append((ts, equity))
        if len(self._equity_curve) > 100_000:
            self._equity_curve = self._equity_curve[-100_000:]

    @staticmethod
    def _classify_asset(symbol: str) -> AssetClass:
        """Heuristic asset class detection. Use an instrument registry in prod."""
        s = symbol.upper()
        if any(s.endswith(q) for q in ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]):
            return AssetClass.FOREX
        if any(s.startswith(c) for c in ["BTC", "ETH", "BNB", "XRP", "SOL", "ADA", "DOT"]):
            return AssetClass.CRYPTO
        if s.endswith("F") or any(x in s for x in ["FUT", "/", "CME", "!"]):
            return AssetClass.FUTURES
        return AssetClass.STOCKS
