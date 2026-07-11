"""
Strategy Engine — Plugin registry, strategy lifecycle, and signal routing.

Design Decision:
    Strategies are first-class plugins discovered via an import-time registry.
    The registry uses the Factory pattern: strategies are registered by ID and
    instantiated on demand with injected dependencies.

    Strategy execution is async to avoid blocking the event loop on
    compute-heavy rule evaluation (delegated to thread pool if needed).

    Multi-timeframe support: strategies subscribe to multiple bar streams
    and receive BarEvents annotated with their timeframe.

Architecture:
    StrategyEngine orchestrates:
        1. Strategy lifecycle (register, activate, deactivate, reset)
        2. Event routing (dispatches BarEvents to all subscribed strategies)
        3. Signal collection and forwarding to the Risk Engine gate

Plugin System:
    @strategy_registry.register("ema_crossover_v1")
    class EMACrossoverStrategy(BaseStrategy): ...

    Strategies auto-register when their module is imported.
    Lazy loading is supported for memory efficiency in backtesting.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Callable, Type

import pandas as pd

from core.domain.events import BarEvent, SignalEvent, TickEvent
from core.interfaces import IIndicator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Registry (Plugin Pattern)
# ─────────────────────────────────────────────────────────────────────────────


class StrategyRegistry:
    """
    Thread-safe strategy registry implementing the Plugin / Factory pattern.

    Strategies register themselves at import time via the @register decorator.
    The registry maps strategy_id → (class, default_config).

    Design Decision:
        Using a class-level dict (not module-level) allows multiple isolated
        registries in tests without global state pollution.
    """

    _registry: dict[str, tuple[Type["BaseStrategy"], dict[str, Any]]] = {}

    def register(
        self,
        strategy_id: str,
        default_config: dict[str, Any] | None = None,
    ) -> Callable:
        """
        Decorator to register a strategy class.

        Args:
            strategy_id: Unique identifier (used in config and API).
            default_config: Default parameter dict for the strategy.

        Example:
            @registry.register("ema_cross_v1", {"fast_period": 9, "slow_period": 21})
            class EMACrossStrategy(BaseStrategy): ...
        """

        def decorator(cls: Type["BaseStrategy"]) -> Type["BaseStrategy"]:
            if strategy_id in self._registry:
                logger.warning(f"Strategy '{strategy_id}' is being re-registered")
            self._registry[strategy_id] = (cls, default_config or {})
            logger.debug(f"Strategy registered: '{strategy_id}'")
            return cls

        return decorator

    def create(
        self,
        strategy_id: str,
        config: dict[str, Any] | None = None,
    ) -> "BaseStrategy":
        """
        Instantiate a registered strategy with optional config override.

        Args:
            strategy_id: Registered strategy identifier.
            config: Override parameters (merged with defaults).

        Returns:
            Configured BaseStrategy instance.

        Raises:
            KeyError: If strategy_id is not registered.
        """
        if strategy_id not in self._registry:
            raise KeyError(
                f"Strategy '{strategy_id}' not found. Available: {list(self._registry.keys())}"
            )
        cls, defaults = self._registry[strategy_id]
        merged = {**defaults, **(config or {})}
        return cls(strategy_id=strategy_id, config=merged)

    def list_registered(self) -> list[str]:
        """Return all registered strategy IDs."""
        return list(self._registry.keys())


# Singleton registry — import this in each strategy module
strategy_registry = StrategyRegistry()


# ─────────────────────────────────────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────────────────────────────────────


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    Subclasses implement on_bar() and optionally on_tick().
    State management, indicator wiring, and logging are provided here.

    Lifecycle:
        __init__ → initialise() → on_bar() / on_tick() → reset() [for backtesting]

    Attributes:
        _strategy_id: Unique strategy identifier.
        _config: Parameter dict (validated against defaults at init).
        _bar_history: Rolling OHLCV buffer per symbol-timeframe key.
        _indicators: Dict of attached indicator instances.
        _position_tracker: Tracks if we are long/flat/short per symbol.
    """

    def __init__(
        self,
        strategy_id: str,
        config: dict[str, Any],
    ) -> None:
        self._strategy_id = strategy_id
        self._config = config
        self._bar_history: dict[str, pd.DataFrame] = {}
        self._indicators: dict[str, IIndicator] = {}
        self._position_tracker: dict[str, str] = defaultdict(lambda: "FLAT")
        self._bar_count: dict[str, int] = defaultdict(int)
        self._is_active: bool = True

        self._validate_config()
        logger.info(
            f"Strategy initialised: {strategy_id}",
            extra={"config": config},
        )

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    @abstractmethod
    def symbols(self) -> list[str]:
        """Instruments this strategy trades."""
        ...

    @property
    @abstractmethod
    def timeframes(self) -> list[str]:
        """Bar timeframes this strategy subscribes to."""
        ...

    @property
    def min_bars_required(self) -> int:
        """Minimum bars before the strategy produces signals (warm-up period)."""
        return max(
            (ind.min_periods for ind in self._indicators.values()),
            default=1,
        )

    # ── Abstract interface ─────────────────────────────────────────────────

    @abstractmethod
    async def _generate_signal(self, symbol: str, df: pd.DataFrame) -> str | None:
        """
        Core signal logic. Returns 'LONG', 'SHORT', 'FLAT', or None (no change).

        Args:
            symbol: The instrument being evaluated.
            df: OHLCV DataFrame with computed indicator columns appended.

        Returns:
            Direction string or None if no action should be taken.
        """
        ...

    def _validate_config(self) -> None:
        """Override to validate config parameters at init time."""
        pass

    # ── Bar processing ─────────────────────────────────────────────────────

    async def on_bar(self, event: BarEvent) -> SignalEvent | None:
        """
        Process a completed bar event and optionally emit a signal.

        Steps:
            1. Append bar to rolling history
            2. Compute all attached indicators
            3. Run _generate_signal() (subclass implementation)
            4. Build and return SignalEvent if direction changed

        Args:
            event: Completed BarEvent (is_closed=True).

        Returns:
            SignalEvent or None.
        """
        if not self._is_active:
            return None
        if event.symbol not in self.symbols:
            return None
        if event.timeframe not in self.timeframes:
            return None

        key = f"{event.symbol}_{event.timeframe}"

        # Append new bar to history buffer
        new_row = pd.DataFrame(
            [
                {
                    "timestamp": event.timestamp,
                    "open": float(event.open),
                    "high": float(event.high),
                    "low": float(event.low),
                    "close": float(event.close),
                    "volume": float(event.volume),
                }
            ]
        ).set_index("timestamp")

        if key not in self._bar_history:
            self._bar_history[key] = new_row
        else:
            self._bar_history[key] = pd.concat([self._bar_history[key], new_row]).tail(
                self._get_history_length()
            )

        self._bar_count[key] += 1
        df = self._bar_history[key].copy()

        # Check warm-up period
        if len(df) < self.min_bars_required:
            return None

        # Compute indicators
        for ind_name, indicator in self._indicators.items():
            try:
                df[ind_name] = indicator.compute(df)
            except Exception as exc:
                logger.error(
                    f"Indicator {ind_name} computation failed: {exc}",
                    extra={"strategy": self._strategy_id, "symbol": event.symbol},
                )
                return None

        # Generate signal
        try:
            direction = await self._generate_signal(event.symbol, df)
        except Exception as exc:
            logger.error(
                f"Signal generation failed: {exc}",
                extra={"strategy": self._strategy_id, "symbol": event.symbol},
                exc_info=True,
            )
            return None

        if direction is None:
            return None

        # Only emit signal if direction changed
        current_position = self._position_tracker[event.symbol]
        if direction == current_position:
            return None

        self._position_tracker[event.symbol] = direction

        signal = SignalEvent(
            source=self._strategy_id,
            symbol=event.symbol,
            strategy_id=self._strategy_id,
            direction=direction,
            strength=self._calculate_strength(df),
            signal_price=event.close,
            correlation_id=event.correlation_id,
            metadata={
                "bar_index": event.bar_index,
                "timeframe": event.timeframe,
                "bar_count": self._bar_count[key],
            },
        )

        logger.info(
            "Signal generated",
            extra={
                "strategy_id": self._strategy_id,
                "symbol": event.symbol,
                "direction": direction,
                "price": str(event.close),
                "strength": signal.strength,
            },
        )
        return signal

    async def on_tick(self, event: TickEvent) -> SignalEvent | None:
        """Default tick handler — no-op for bar-based strategies."""
        return None

    def attach_indicator(self, name: str, indicator: IIndicator) -> "BaseStrategy":
        """
        Attach an indicator to this strategy (fluent builder pattern).

        Args:
            name: Column name the indicator values will use in the DataFrame.
            indicator: IIndicator implementation.

        Returns:
            Self for method chaining.
        """
        self._indicators[name] = indicator
        return self

    def reset(self) -> None:
        """Reset all stateful data for backtesting reruns."""
        self._bar_history.clear()
        self._position_tracker = defaultdict(lambda: "FLAT")
        self._bar_count.clear()
        logger.debug(f"Strategy {self._strategy_id} reset")

    def _get_history_length(self) -> int:
        """Rolling window size to keep in memory (prevents unbounded growth)."""
        return max(self.min_bars_required * 3, 500)

    def _calculate_strength(self, df: pd.DataFrame) -> float:
        """
        Default strength calculation: 1.0 (full conviction).
        Override in subclasses for dynamic conviction scaling.
        """
        return 1.0

    def deactivate(self) -> None:
        """Pause signal generation without destroying state."""
        self._is_active = False
        logger.info(f"Strategy {self._strategy_id} deactivated")

    def activate(self) -> None:
        """Resume signal generation."""
        self._is_active = True
        logger.info(f"Strategy {self._strategy_id} activated")


# ─────────────────────────────────────────────────────────────────────────────
# EMA Crossover — Concrete Strategy Example
# ─────────────────────────────────────────────────────────────────────────────


@strategy_registry.register(
    "ema_crossover_v1",
    {"fast_period": 9, "slow_period": 21, "atr_period": 14, "min_atr_mult": 0.5},
)
class EMACrossoverStrategy(BaseStrategy):
    """
    Trend-following strategy using EMA crossover with ATR filter.

    Entry Logic:
        LONG:  fast_ema crosses above slow_ema AND close > slow_ema * (1 + min_atr_filter)
        SHORT: fast_ema crosses below slow_ema AND close < slow_ema * (1 - min_atr_filter)
        FLAT:  Opposing crossover detected

    Exit Logic:
        Position is closed on opposing crossover signal.
        No separate stop-loss — relies on Risk Engine position limits.

    Config:
        fast_period: Fast EMA period (default 9).
        slow_period: Slow EMA period (default 21).
        atr_period: ATR period for volatility filter (default 14).
        min_atr_mult: Minimum ATR as fraction of price for signal validity.
    """

    @property
    def symbols(self) -> list[str]:
        return self._config.get("symbols", ["EURUSD", "GBPUSD"])

    @property
    def timeframes(self) -> list[str]:
        return self._config.get("timeframes", ["H1"])

    @property
    def min_bars_required(self) -> int:
        return max(self._config["slow_period"] * 2, self._config["atr_period"] * 2)

    def _validate_config(self) -> None:
        fast = self._config.get("fast_period", 9)
        slow = self._config.get("slow_period", 21)
        if fast >= slow:
            raise ValueError(f"fast_period ({fast}) must be < slow_period ({slow})")

    async def _generate_signal(self, symbol: str, df: pd.DataFrame) -> str | None:
        """
        Detect EMA crossover with ATR volatility filter.

        Uses the prior two bars to detect the crossover moment:
            prev: fast < slow (bearish alignment)
            curr: fast > slow (bullish crossover)
        """
        if len(df) < 3:
            return None

        fast_col = f"ema_{self._config['fast_period']}"
        slow_col = f"ema_{self._config['slow_period']}"
        atr_col = f"atr_{self._config['atr_period']}"

        # Require all indicators computed
        for col in [fast_col, slow_col, atr_col]:
            if col not in df.columns or df[col].iloc[-1] != df[col].iloc[-1]:  # nan check
                return None

        fast_now = df[fast_col].iloc[-1]
        fast_prev = df[fast_col].iloc[-2]
        slow_now = df[slow_col].iloc[-1]
        slow_prev = df[slow_col].iloc[-2]
        atr_now = df[atr_col].iloc[-1]
        close_now = df["close"].iloc[-1]

        # ATR filter: signal only valid if ATR exceeds minimum threshold
        atr_filter = (atr_now / close_now) >= self._config.get("min_atr_mult", 0.0005)
        if not atr_filter:
            return None

        # Bullish crossover: fast crossed above slow
        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        # Bearish crossover: fast crossed below slow
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

        if bullish_cross:
            return "LONG"
        elif bearish_cross:
            return "SHORT"
        return None

    def _calculate_strength(self, df: pd.DataFrame) -> float:
        """
        Conviction = normalised gap between fast and slow EMA.
        Larger gap = higher conviction (capped at 1.0).
        """
        fast_col = f"ema_{self._config['fast_period']}"
        slow_col = f"ema_{self._config['slow_period']}"

        if fast_col not in df.columns or slow_col not in df.columns:
            return 1.0

        fast = df[fast_col].iloc[-1]
        slow = df[slow_col].iloc[-1]

        if slow == 0:
            return 1.0

        gap_pct = abs(fast - slow) / slow
        # Normalise: 0.1% gap = 0.5 strength, 0.2% gap = 1.0 (capped)
        return min(1.0, gap_pct / 0.002)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Engine (Orchestrator)
# ─────────────────────────────────────────────────────────────────────────────


class StrategyEngine:
    """
    Manages the lifecycle of active strategies and routes market events.

    Responsibilities:
        1. Register/activate/deactivate strategies
        2. Receive BarEvents from the Indicator Engine
        3. Fan-out events to all subscribed strategies in parallel
        4. Collect resulting signals and forward to the Risk Engine gate

    Args:
        event_bus: For subscribing to bar events and publishing signals.
        risk_engine: Risk validation gate (must approve before publication).
    """

    def __init__(
        self,
        event_bus: object,
        risk_engine: object,
    ) -> None:
        self._event_bus = event_bus
        self._risk_engine = risk_engine
        self._active_strategies: dict[str, BaseStrategy] = {}
        self._symbol_index: dict[str, list[str]] = defaultdict(list)

        logger.info("Strategy Engine initialised")

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """
        Add a strategy to the active pool.

        Args:
            strategy: Initialised strategy instance.
        """
        sid = strategy.strategy_id
        self._active_strategies[sid] = strategy
        for symbol in strategy.symbols:
            if sid not in self._symbol_index[symbol]:
                self._symbol_index[symbol].append(sid)
        logger.info(
            f"Strategy registered: {sid}",
            extra={"symbols": strategy.symbols, "timeframes": strategy.timeframes},
        )

    def deregister_strategy(self, strategy_id: str) -> None:
        """Remove a strategy from the active pool."""
        if strategy_id not in self._active_strategies:
            return
        strategy = self._active_strategies.pop(strategy_id)
        for symbol in strategy.symbols:
            self._symbol_index[symbol] = [s for s in self._symbol_index[symbol] if s != strategy_id]
        logger.info(f"Strategy deregistered: {strategy_id}")

    async def on_bar(self, event: BarEvent) -> list[SignalEvent]:
        """
        Dispatch a bar event to all strategies subscribed to its symbol.

        Strategies run concurrently (asyncio.gather) for throughput.
        Risk gate is applied to each signal individually.

        Args:
            event: Completed BarEvent.

        Returns:
            List of risk-approved SignalEvents (may be empty).
        """
        strategy_ids = self._symbol_index.get(event.symbol, [])
        if not strategy_ids:
            return []

        # Fan-out to all matching strategies
        tasks = [
            self._active_strategies[sid].on_bar(event)
            for sid in strategy_ids
            if sid in self._active_strategies
        ]

        raw_signals: list[SignalEvent | None] = await asyncio.gather(
            *tasks, return_exceptions=False
        )

        approved_signals: list[SignalEvent] = []
        for signal in raw_signals:
            if signal is None:
                continue
            # Gate through risk engine
            if hasattr(self._risk_engine, "validate_signal"):
                try:
                    approved = await self._risk_engine.validate_signal(signal)
                    if approved:
                        approved_signals.append(signal)
                except Exception as exc:
                    logger.error(
                        f"Risk validation error for {signal.symbol}: {exc}",
                        exc_info=True,
                    )
            else:
                approved_signals.append(signal)

        # Publish approved signals to the event bus
        for signal in approved_signals:
            try:
                if hasattr(self._event_bus, "publish"):
                    await self._event_bus.publish(signal)
            except Exception as exc:
                logger.error(f"Failed to publish signal: {exc}", exc_info=True)

        return approved_signals

    def get_active_strategies(self) -> dict[str, dict]:
        """Return summary of all active strategies."""
        return {
            sid: {
                "symbols": strat.symbols,
                "timeframes": strat.timeframes,
                "is_active": strat._is_active,
                "position_tracker": dict(strat._position_tracker),
            }
            for sid, strat in self._active_strategies.items()
        }

    def reset_all(self) -> None:
        """Reset all strategies (used between backtest runs)."""
        for strategy in self._active_strategies.values():
            strategy.reset()
        logger.info("All strategies reset")
