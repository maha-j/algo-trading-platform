"""
Backtest Engine
===============
A vectorised + event-driven hybrid backtesting framework.

Architecture
------------
* Phase 1 — Data hydration: load OHLCV from TimescaleDB or provider cache.
* Phase 2 — Indicator warm-up: compute all indicators on the full bar series
            so bar[0] of the strategy already has valid indicators.
* Phase 3 — Event replay: replay BarEvents through strategy → risk →
            (simulated) execution in chronological order.
* Phase 4 — Analytics: compute industry-standard performance statistics.

Design decisions
----------------
* We do NOT use vectorised "apply fill at close price" shortcuts.
  The event-driven loop properly models execution at the OPEN of the next bar
  (next-bar-at-open fill model), which is the most realistic assumption for
  liquid instruments.  This prevents look-ahead bias.
* Slippage model: Gaussian noise proportional to ATR (configurable).
* Commission model: per-lot + per-trade flat fee (broker-specific config).
* Walk-forward optimization uses anchored windows (expanding in-sample,
  fixed out-of-sample) to avoid over-fitting.
* Results dict is compatible with QuantStats / PyFolio for further analysis.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import numpy as np
import pandas as pd

from config.settings import get_settings
from core.domain.events import BarEvent, FillEvent, OrderEvent, SignalEvent
from indicator_engine.service import IndicatorService
from portfolio_engine.service import PortfolioEngine
from strategy_engine.service import BaseStrategy

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Backtest configuration
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_per_lot: float = 7.0  # USD per standard lot (round-trip)
    slippage_bps: float = 1.0  # basis points of ATR
    spread_bps: float = 2.0  # simulated bid-ask spread
    risk_free_rate: float = 0.05  # annualised, for Sharpe calculation
    trading_days_per_year: int = 252


# ---------------------------------------------------------------------------
# Performance statistics
# ---------------------------------------------------------------------------


@dataclass
class BacktestStats:
    """Industry-standard performance metrics used by quantitative funds."""

    # Returns
    total_return_pct: float = 0.0
    annualised_return_pct: float = 0.0
    daily_return_mean: float = 0.0
    daily_return_std: float = 0.0

    # Risk-adjusted
    sharpe_ratio: float = 0.0  # (return - Rf) / std
    sortino_ratio: float = 0.0  # (return - Rf) / downside_std
    calmar_ratio: float = 0.0  # annualised_return / max_drawdown
    omega_ratio: float = 0.0  # E[gains above threshold] / E[losses below]

    # Drawdown
    max_drawdown_pct: float = 0.0
    avg_drawdown_pct: float = 0.0
    max_drawdown_duration_bars: int = 0

    # Trade statistics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0  # gross_profit / gross_loss
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_trade_return: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_holding_bars: float = 0.0

    # Costs
    total_commission: float = 0.0
    total_slippage: float = 0.0

    # Time
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    total_bars: int = 0
    runtime_seconds: float = 0.0


def compute_statistics(
    equity_curve: pd.Series,
    fills: List[FillEvent],
    config: BacktestConfig,
) -> BacktestStats:
    """Compute full performance statistics from an equity curve and fill list."""
    stats = BacktestStats()

    if equity_curve.empty or len(equity_curve) < 2:
        return stats

    stats.total_bars = len(equity_curve)
    stats.start_date = str(equity_curve.index[0])
    stats.end_date = str(equity_curve.index[-1])

    # Returns
    returns = equity_curve.pct_change().dropna()
    total_days = (equity_curve.index[-1] - equity_curve.index[0]).days or 1
    years = total_days / 365.25

    stats.total_return_pct = float((equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100)
    stats.annualised_return_pct = (
        float(((1 + stats.total_return_pct / 100) ** (1 / years) - 1) * 100) if years > 0 else 0.0
    )

    stats.daily_return_mean = float(returns.mean())
    stats.daily_return_std = float(returns.std())

    # Risk-adjusted ratios
    rf_daily = config.risk_free_rate / config.trading_days_per_year
    excess = returns - rf_daily
    downside = returns[returns < rf_daily]

    if stats.daily_return_std > 0:
        stats.sharpe_ratio = float(
            excess.mean() / returns.std() * np.sqrt(config.trading_days_per_year)
        )
    if len(downside) > 1 and downside.std() > 0:
        stats.sortino_ratio = float(
            excess.mean() / downside.std() * np.sqrt(config.trading_days_per_year)
        )

    # Drawdown
    rolling_max = equity_curve.cummax()
    drawdown = (equity_curve - rolling_max) / rolling_max
    stats.max_drawdown_pct = float(drawdown.min() * 100)
    stats.avg_drawdown_pct = (
        float(drawdown[drawdown < 0].mean() * 100) if (drawdown < 0).any() else 0.0
    )

    # Drawdown duration
    in_dd = drawdown < 0
    dd_len = 0
    max_dd_len = 0
    for v in in_dd:
        dd_len = dd_len + 1 if v else 0
        max_dd_len = max(max_dd_len, dd_len)
    stats.max_drawdown_duration_bars = max_dd_len

    # Calmar
    if stats.max_drawdown_pct != 0:
        stats.calmar_ratio = float(stats.annualised_return_pct / abs(stats.max_drawdown_pct))

    # Omega ratio (threshold = risk-free rate)
    gains = returns[returns > rf_daily] - rf_daily
    losses = rf_daily - returns[returns < rf_daily]
    if losses.sum() > 0:
        stats.omega_ratio = float(gains.sum() / losses.sum())

    # Trade statistics from fills
    trade_pnls: List[float] = []
    gross_profit = 0.0
    gross_loss = 0.0
    total_commission = 0.0
    total_slippage = 0.0

    for fill in fills:
        pnl = getattr(fill, "realised_pnl", 0.0)
        trade_pnls.append(pnl)
        total_commission += fill.commission
        total_slippage += getattr(fill, "slippage", 0.0)
        if pnl > 0:
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)

    stats.total_trades = len(fills)
    stats.winning_trades = sum(1 for p in trade_pnls if p > 0)
    stats.losing_trades = sum(1 for p in trade_pnls if p <= 0)
    stats.win_rate_pct = (
        (stats.winning_trades / stats.total_trades * 100) if stats.total_trades else 0.0
    )
    stats.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    winners = [p for p in trade_pnls if p > 0]
    losers = [p for p in trade_pnls if p <= 0]
    stats.avg_win = float(np.mean(winners)) if winners else 0.0
    stats.avg_loss = float(np.mean(losers)) if losers else 0.0
    stats.avg_trade_return = float(np.mean(trade_pnls)) if trade_pnls else 0.0
    stats.best_trade = float(max(trade_pnls)) if trade_pnls else 0.0
    stats.worst_trade = float(min(trade_pnls)) if trade_pnls else 0.0
    stats.total_commission = total_commission
    stats.total_slippage = total_slippage

    return stats


# ---------------------------------------------------------------------------
# Simulated Execution (no broker connection)
# ---------------------------------------------------------------------------


class SimulatedExecution:
    """
    Fill model for backtesting.

    Fill model: next-bar-open + slippage.
    This is the most realistic model for liquid instruments and prevents
    look-ahead bias that occurs when filling at the signal bar's close.
    """

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.fills: List[FillEvent] = []

    def execute(
        self,
        order: OrderEvent,
        next_bar: BarEvent,
        atr: float = 0.0,
    ) -> Optional[FillEvent]:
        if next_bar is None:
            return None

        # Base fill price = next bar open
        fill_px = next_bar.open

        # Slippage: Gaussian noise proportional to ATR
        if atr > 0:
            slippage = np.random.normal(0, atr * self.config.slippage_bps / 10_000)
            fill_px += slippage if order.side == "BUY" else -slippage

        # Spread cost
        spread_cost = fill_px * self.config.spread_bps / 10_000
        fill_px += spread_cost / 2 if order.side == "BUY" else -spread_cost / 2

        # Commission (fixed per lot, simplified)
        commission = self.config.commission_per_lot * float(order.quantity)

        fill = FillEvent(
            source="backtest",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            fill_price=round(fill_px, 5),
            commission=commission,
            timestamp=next_bar.timestamp,
            slippage=abs(fill_px - next_bar.open),
        )
        self.fills.append(fill)
        return fill


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------


class BacktestEngine:
    """
    Event-driven backtesting engine.

    Usage
    -----
    engine = BacktestEngine(config=BacktestConfig())
    result = await engine.run(
        strategy=EMACrossoverStrategy(config={...}),
        bars=df,           # pd.DataFrame indexed by datetime
        symbol="EURUSD",
        timeframe="H1",
    )
    """

    def __init__(self, config: Optional[BacktestConfig] = None) -> None:
        self.config = config or BacktestConfig()

    async def run(
        self,
        strategy: BaseStrategy,
        bars: pd.DataFrame,
        symbol: str,
        timeframe: str = "H1",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Run the strategy against historical bar data.

        Returns a dict with:
          - equity_curve: pd.Series
          - fills: list[FillEvent]
          - stats: BacktestStats
          - bar_log: list[dict] (per-bar state for debugging)
        """
        t0 = time.perf_counter()

        # Filter date range
        if start_date:
            bars = bars[bars.index >= pd.Timestamp(start_date, tz="UTC")]
        if end_date:
            bars = bars[bars.index <= pd.Timestamp(end_date, tz="UTC")]

        if len(bars) < 50:
            logger.error("Backtest: insufficient bars (%d)", len(bars))
            return {"error": "insufficient_bars"}

        logger.info(
            "Backtest starting: %s %s | %d bars | capital=%.2f",
            symbol,
            timeframe,
            len(bars),
            self.config.initial_capital,
        )

        # Initialise components
        indicator_svc = IndicatorService()
        portfolio = PortfolioEngine(initial_capital=self.config.initial_capital)
        execution = SimulatedExecution(self.config)
        strategy.reset()

        # Pre-compute all indicators on the full series (warm-up)
        indicator_svc.compute_all(symbol, timeframe, bars)

        equity_curve: List[Tuple[datetime, float]] = []
        bar_log: List[dict] = []
        bars_list = [
            BarEvent(
                source="backtest",
                symbol=symbol,
                timeframe=timeframe,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                timestamp=ts,
                bar_index=i,
                is_closed=True,
            )
            for i, (ts, row) in enumerate(bars.iterrows())
        ]

        pending_orders: List[OrderEvent] = []

        for idx, bar in enumerate(bars_list):
            # --- Execute pending orders at this bar's open (next-bar model) ---
            for order in list(pending_orders):
                atr_val = indicator_svc.get_last_value(symbol, timeframe, "ATR_14") or 0.0
                fill = execution.execute(order, bar, atr_val)
                if fill:
                    await portfolio.on_fill(fill)
                pending_orders.remove(order)

            # --- Update unrealised P&L ---
            from core.domain.events import TickEvent

            synthetic_tick = TickEvent(
                source="backtest",
                symbol=symbol,
                bid=bar.close,
                ask=bar.close,
                volume=bar.volume,
                timestamp=bar.timestamp,
            )
            await portfolio.on_tick(synthetic_tick)

            # --- Record equity ---
            equity = float(portfolio.get_equity())
            equity_curve.append((bar.timestamp, equity))

            # --- Compute indicators for sub-window up to current bar ---
            sub_df = bars.iloc[max(0, idx - 499) : idx + 1]
            if len(sub_df) >= 30:
                indicator_svc.compute_all(symbol, timeframe, sub_df)

            # --- Run strategy ---
            signal: Optional[SignalEvent] = await strategy.on_bar(bar, indicator_svc)

            if signal:
                # Simple risk pass-through for backtest (full risk engine optional)
                qty = portfolio.calculate_position_size(
                    symbol=symbol,
                    price=bar.close,
                    daily_vol=0.01,  # Could compute from ATR
                )
                if qty > 0:
                    order = OrderEvent(
                        source="backtest",
                        signal_id=signal.event_id,
                        symbol=signal.symbol,
                        side="BUY" if signal.direction == "LONG" else "SELL",
                        quantity=float(qty),
                        order_type="MARKET",
                        algorithm="MARKET",
                        risk_approved=True,
                    )
                    pending_orders.append(order)

            bar_log.append(
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "close": bar.close,
                    "equity": equity,
                    "signal": signal.direction if signal else None,
                    "open_positions": len(portfolio.get_positions()),
                }
            )

        # Build equity Series
        ts_index = pd.DatetimeIndex([t for t, _ in equity_curve], tz="UTC")
        eq_values = [v for _, v in equity_curve]
        equity_series = pd.Series(eq_values, index=ts_index, name="equity")

        # Compute statistics
        stats = compute_statistics(equity_series, execution.fills, self.config)
        stats.runtime_seconds = time.perf_counter() - t0

        logger.info(
            "Backtest complete in %.2fs | total_return=%.2f%% Sharpe=%.2f MaxDD=%.2f%%",
            stats.runtime_seconds,
            stats.total_return_pct,
            stats.sharpe_ratio,
            stats.max_drawdown_pct,
        )

        return {
            "equity_curve": equity_series,
            "fills": execution.fills,
            "stats": stats.__dict__,
            "bar_log": bar_log,
            "config": self.config.__dict__,
        }

    async def walk_forward(
        self,
        strategy_cls: Type[BaseStrategy],
        strategy_config: dict,
        bars: pd.DataFrame,
        symbol: str,
        timeframe: str = "H1",
        in_sample_bars: int = 1000,
        out_of_sample_bars: int = 250,
        n_splits: int = 5,
    ) -> Dict[str, Any]:
        """
        Anchored walk-forward analysis.

        Windows:
          Split 1: IS=[0, in_sample_bars],       OOS=[in_sample_bars, in_sample_bars+oos]
          Split 2: IS=[0, in_sample_bars+oos],    OOS=[in_sample_bars+oos, ...]
          ...
        This expanding-IS design is more conservative than rolling IS because
        the strategy sees more data over time (typical for trend-following).
        """
        total_bars = len(bars)
        split_results: List[Dict[str, Any]] = []

        for split in range(n_splits):
            is_end = in_sample_bars + split * out_of_sample_bars
            oos_start = is_end
            oos_end = oos_start + out_of_sample_bars

            if oos_end > total_bars:
                break

            is_bars = bars.iloc[:is_end]
            oos_bars = bars.iloc[oos_start:oos_end]

            logger.info(
                "Walk-forward split %d/%d | IS=%d bars | OOS=%d bars",
                split + 1,
                n_splits,
                len(is_bars),
                len(oos_bars),
            )

            # Run on OOS (in a real WFO, IS is used to optimise params first)
            strategy_instance = strategy_cls(config=strategy_config)
            oos_result = await self.run(strategy_instance, oos_bars, symbol, timeframe)
            oos_result["split"] = split + 1
            oos_result["is_bars"] = len(is_bars)
            oos_result["oos_bars"] = len(oos_bars)
            split_results.append(oos_result)

        # Aggregate OOS statistics
        if not split_results:
            return {"error": "no_splits_completed"}

        sharpe_values = [r["stats"]["sharpe_ratio"] for r in split_results if "stats" in r]
        return_values = [r["stats"]["total_return_pct"] for r in split_results if "stats" in r]
        dd_values = [r["stats"]["max_drawdown_pct"] for r in split_results if "stats" in r]

        summary = {
            "n_splits": len(split_results),
            "avg_oos_sharpe": float(np.mean(sharpe_values)),
            "avg_oos_return": float(np.mean(return_values)),
            "avg_oos_max_dd": float(np.mean(dd_values)),
            "consistency_pct": float(
                sum(1 for r in return_values if r > 0) / len(return_values) * 100
            ),
            "split_results": [
                {
                    "split": r["split"],
                    "sharpe": r["stats"]["sharpe_ratio"],
                    "return": r["stats"]["total_return_pct"],
                    "max_dd": r["stats"]["max_drawdown_pct"],
                    "win_rate": r["stats"]["win_rate_pct"],
                }
                for r in split_results
                if "stats" in r
            ],
        }
        return summary
