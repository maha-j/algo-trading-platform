"""
Risk Engine — Portfolio risk gating, VaR, CVaR, and circuit breakers.

Fixes applied (BUG-04, BUG-05, BUG-06, FAULT-03, CONTRACT-01):
  - BUG-04: DailyLossValidator now calls portfolio.get_daily_pnl() (correct
            daily scope) instead of get_realised_pnl() (cumulative, wrong).
  - BUG-05: datetime.utcnow() → datetime.now(timezone.utc) everywhere.
  - BUG-06: validate_signal() signature unified — takes only (signal) per
            Protocol contract; validators internally query portfolio state.
  - FAULT-03: CircuitBreaker now has HALF_OPEN state with auto-recovery timer.
  - CONTRACT-01: Protocol methods called correctly (sync where impl is sync).

Design Decision:
    Chain of Responsibility for composable risk validation.
    Each RiskValidator is independent and can be enabled/disabled per config.

    Validators run in priority order:
        1. PositionLimitValidator    — hard size cap per symbol
        2. MaxOpenPositionsValidator — max concurrent positions
        3. DailyLossValidator        — stop trading on daily loss breach
        4. VaRValidator              — portfolio VaR threshold
        5. DrawdownValidator         — max drawdown circuit breaker

Performance:
    VaR calculation runs in ThreadPoolExecutor (numpy-heavy, CPU-bound).
    Results cached; risk engine processes signals asynchronously.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from config.settings import RiskSettings, get_settings
from core.domain.events import RiskBreachEvent, SignalEvent
from core.interfaces import IPortfolioEngine

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Risk Validation Result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationResult:
    """
    Immutable result from a single risk validator.

    Attributes:
        approved:       True if the signal passes this validator.
        validator_name: Class name of the validator.
        message:        Human-readable explanation (always set).
        metric_value:   Current value of the checked metric.
        limit_value:    Configured limit for reference.
    """

    approved: bool
    validator_name: str
    message: str
    metric_value: float = 0.0
    limit_value: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Abstract Validator (Chain of Responsibility)
# ─────────────────────────────────────────────────────────────────────────────


class RiskValidator(ABC):
    """Abstract base for all risk validators in the chain."""

    def __init__(self, settings: RiskSettings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        """
        Evaluate signal against this validator's risk criterion.

        Args:
            signal:          Candidate signal from the strategy engine.
            portfolio:       Live portfolio state reader.
            returns_history: Daily returns Series (most recent N days).

        Returns:
            ValidationResult with approval/rejection decision.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Concrete Validators
# ─────────────────────────────────────────────────────────────────────────────


class PositionLimitValidator(RiskValidator):
    """
    Reject signals that would exceed the max position size per symbol.
    Checks: current_position_value / total_equity <= max_position_size_pct
    """

    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        # CONTRACT-01 fix: get_equity() is sync in implementation
        equity = float(portfolio.get_equity())
        if equity <= 0:
            return ValidationResult(
                approved=False,
                validator_name=self.name,
                message="Zero equity — cannot size position",
                limit_value=self._settings.max_position_size_pct,
            )

        positions = portfolio.get_positions()
        current_pos = positions.get(signal.symbol)
        current_mv = abs(float(current_pos.market_value)) if current_pos else 0.0
        current_pct = current_mv / equity
        limit = self._settings.max_position_size_pct
        approved = current_pct < limit

        return ValidationResult(
            approved=approved,
            validator_name=self.name,
            message=(f"Position {signal.symbol}: {current_pct:.2%} vs limit {limit:.2%}"),
            metric_value=current_pct,
            limit_value=limit,
        )


class MaxOpenPositionsValidator(RiskValidator):
    """Reject new signals if the max open position count is reached."""

    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        # FLAT signals always pass — we must allow closes
        if signal.direction == "FLAT":
            return ValidationResult(
                approved=True,
                validator_name=self.name,
                message="Exit signal — position count check bypassed",
            )

        positions = portfolio.get_positions()
        open_count = len(positions)
        limit = self._settings.max_open_positions
        # Already has a position in this symbol → not adding a new one
        already_has = signal.symbol in positions
        effective_new = open_count if already_has else open_count + 1
        approved = effective_new <= limit

        return ValidationResult(
            approved=approved,
            validator_name=self.name,
            message=f"Open positions: {open_count}/{limit}",
            metric_value=float(open_count),
            limit_value=float(limit),
        )


class DailyLossValidator(RiskValidator):
    """
    Halt trading if daily loss exceeds the configured threshold.

    FIX BUG-04: Uses portfolio.get_daily_pnl() (UTC calendar day P&L)
    instead of get_realised_pnl() (cumulative all-time P&L, which never
    correctly reflected the daily loss limit).
    """

    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        equity = float(portfolio.get_equity())
        if equity <= 0:
            return ValidationResult(
                approved=False,
                validator_name=self.name,
                message="Cannot calculate daily loss without equity",
            )

        # FIX BUG-04: daily scope, not cumulative
        daily_pnl = float(portfolio.get_daily_pnl())
        daily_loss_pct = min(0.0, daily_pnl / equity)  # negative = loss
        limit = -self._settings.max_daily_loss_pct
        approved = daily_loss_pct > limit

        return ValidationResult(
            approved=approved,
            validator_name=self.name,
            message=(f"Daily P&L: {daily_pnl:+.2f} ({daily_loss_pct:.2%}) | Limit: {limit:.2%}"),
            metric_value=daily_loss_pct,
            limit_value=limit,
        )


class VaRValidator(RiskValidator):
    """
    Validate that portfolio VaR does not exceed the configured limit.

    Historical Simulation VaR:
        - 252-day returns lookback
        - 99% confidence level (1% tail)
        - 1-day horizon
    """

    def __init__(self, settings: RiskSettings, executor: ThreadPoolExecutor) -> None:
        super().__init__(settings)
        self._executor = executor

    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        if len(returns_history) < 30:
            logger.warning("Insufficient return history for VaR calculation")
            return ValidationResult(
                approved=True,
                validator_name=self.name,
                message="Insufficient history — VaR check skipped",
            )

        loop = asyncio.get_event_loop()
        var_pct = await loop.run_in_executor(
            self._executor,
            self._compute_historical_var,
            returns_history,
            self._settings.var_confidence_level,
        )

        equity = float(portfolio.get_equity())
        var_abs = var_pct * equity
        limit = self._settings.max_portfolio_var_pct
        approved = var_pct <= limit

        return ValidationResult(
            approved=approved,
            validator_name=self.name,
            message=(
                f"1-day {self._settings.var_confidence_level:.0%} VaR = "
                f"{var_pct:.3%} (${var_abs:,.0f}) | Limit: {limit:.3%}"
            ),
            metric_value=var_pct,
            limit_value=limit,
        )

    @staticmethod
    def _compute_historical_var(returns: pd.Series, confidence: float) -> float:
        """Historical Simulation VaR (CPU-bound — runs in thread pool)."""
        if returns.empty or returns.std() == 0:
            return 0.0
        percentile = (1.0 - confidence) * 100
        return abs(float(np.percentile(returns.dropna().values, percentile)))


class DrawdownValidator(RiskValidator):
    """
    Block new orders if current drawdown exceeds the maximum allowed.
    FLATTEN action emitted — triggers automatic position reduction.
    """

    async def validate(
        self,
        signal: SignalEvent,
        portfolio: IPortfolioEngine,
        returns_history: pd.Series,
    ) -> ValidationResult:
        if returns_history.empty:
            return ValidationResult(
                approved=True,
                validator_name=self.name,
                message="No history — drawdown check skipped",
            )

        cum_returns = (1 + returns_history).cumprod()
        peak = cum_returns.cummax()
        dd_series = (cum_returns - peak) / peak
        current_dd = abs(float(dd_series.iloc[-1]))
        limit = self._settings.max_drawdown_pct
        approved = current_dd < limit

        return ValidationResult(
            approved=approved,
            validator_name=self.name,
            message=(f"Drawdown {current_dd:.2%} vs limit {limit:.2%}"),
            metric_value=current_dd,
            limit_value=limit,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breaker — FAULT-03: Added HALF_OPEN state with auto-recovery
# ─────────────────────────────────────────────────────────────────────────────


class CircuitBreaker:
    """
    Three-state circuit breaker for trading halt management.

    FIX FAULT-03: Added HALF_OPEN state with configurable recovery window.

    States:
        CLOSED    — Normal operation; all orders processed.
        OPEN      — Triggered by risk breach; all new orders blocked.
        HALF_OPEN — After half_open_after seconds, allows one probe trade
                    to test if conditions have recovered.

    Transition diagram:
        CLOSED ──(breach)──► OPEN
        OPEN   ──(timer)───► HALF_OPEN
        HALF_OPEN ─(ok)───► CLOSED
        HALF_OPEN ─(fail)──► OPEN
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, half_open_after_seconds: float = 3600.0) -> None:
        """
        Args:
            half_open_after_seconds: Seconds after tripping before HALF_OPEN.
                                     Default 1 hour.
        """
        self._state: str = self.CLOSED
        self._triggered_at: Optional[datetime] = None
        self._breach_count: int = 0
        self._half_open_after: timedelta = timedelta(seconds=half_open_after_seconds)
        self._half_open_probe_sent: bool = False

    @property
    def state(self) -> str:
        """Current state, auto-transitioning OPEN → HALF_OPEN on timer."""
        if self._state == self.OPEN and self._triggered_at is not None:
            elapsed = datetime.now(timezone.utc) - self._triggered_at
            if elapsed >= self._half_open_after:
                self._state = self.HALF_OPEN
                self._half_open_probe_sent = False
                logger.warning(
                    "Circuit breaker → HALF_OPEN (recovery probe allowed)",
                    extra={"breach_count": self._breach_count, "elapsed": str(elapsed)},
                )
        return self._state

    @property
    def is_open(self) -> bool:
        """True if trading is fully blocked (OPEN state)."""
        return self.state == self.OPEN

    @property
    def is_half_open(self) -> bool:
        return self.state == self.HALF_OPEN

    def can_probe(self) -> bool:
        """HALF_OPEN: allow exactly one probe trade before confirming recovery."""
        if self.state == self.HALF_OPEN and not self._half_open_probe_sent:
            self._half_open_probe_sent = True
            return True
        return False

    def confirm_recovery(self, operator_id: str = "auto") -> None:
        """Call after a successful probe trade to close the breaker."""
        logger.warning(
            "Circuit breaker → CLOSED (recovery confirmed)",
            extra={"operator_id": operator_id, "breach_count": self._breach_count},
        )
        self._state = self.CLOSED
        self._triggered_at = None
        self._half_open_probe_sent = False

    def trip(self, reason: str) -> None:
        """Open the circuit breaker, halting all new order submission."""
        self._state = self.OPEN
        # FIX BUG-05: timezone-aware timestamp
        self._triggered_at = datetime.now(timezone.utc)
        self._breach_count += 1
        logger.critical(
            "CIRCUIT BREAKER TRIPPED",
            extra={
                "reason": reason,
                "breach_count": self._breach_count,
                "triggered_at": self._triggered_at.isoformat(),
            },
        )

    def reset(self, operator_id: str) -> None:
        """Manual reset by an authorised operator."""
        logger.warning(
            "Circuit breaker manually reset",
            extra={
                "operator_id": operator_id,
                "previous_state": self._state,
                "breach_count": self._breach_count,
            },
        )
        self._state = self.CLOSED
        self._triggered_at = None
        self._half_open_probe_sent = False


# ─────────────────────────────────────────────────────────────────────────────
# Risk Engine (Orchestrator)
# ─────────────────────────────────────────────────────────────────────────────


class RiskEngine:
    """
    Orchestrates all risk validators and the circuit breaker.

    FIX BUG-06: validate_signal() signature aligned with Protocol:
        async def validate_signal(self, signal: SignalEvent) -> bool
    Portfolio is injected at construction; not passed per-call.

    Args:
        portfolio:        Portfolio engine for live state queries.
        event_bus:        Event bus for emitting breach events.
        settings:         Risk configuration parameters.
        returns_provider: Callable returning recent daily returns Series.
    """

    def __init__(
        self,
        portfolio: IPortfolioEngine,
        event_bus: object,
        settings: Optional[RiskSettings] = None,
        returns_provider: Optional[Callable] = None,
    ) -> None:
        self._portfolio = portfolio
        self._event_bus = event_bus
        self._settings = settings or get_settings().risk
        self._returns_provider = returns_provider
        self._circuit_breaker = CircuitBreaker(half_open_after_seconds=3600.0)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="risk-var")

        self._validators: list[RiskValidator] = [
            PositionLimitValidator(self._settings),
            MaxOpenPositionsValidator(self._settings),
            DailyLossValidator(self._settings),
            VaRValidator(self._settings, self._executor),
            DrawdownValidator(self._settings),
        ]

        logger.info(
            "Risk Engine initialised",
            extra={
                "validators": [v.name for v in self._validators],
                "max_drawdown": self._settings.max_drawdown_pct,
                "var_confidence": self._settings.var_confidence_level,
                "max_daily_loss": self._settings.max_daily_loss_pct,
            },
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def validate_signal(self, signal: SignalEvent) -> bool:
        """
        FIX BUG-06: Signature aligned with IEventBus Protocol.
        Runs the full validation chain against a signal.

        Args:
            signal: Candidate signal from the Strategy Engine.

        Returns:
            True if all validators approve; False if any veto.
        """
        # Check circuit breaker first
        if self._circuit_breaker.is_open:
            logger.warning(
                "Signal rejected: circuit breaker OPEN",
                extra={"signal_id": signal.event_id, "symbol": signal.symbol},
            )
            return False

        # HALF_OPEN: allow exactly one probe through
        if self._circuit_breaker.is_half_open:
            if not self._circuit_breaker.can_probe():
                logger.warning("Circuit breaker HALF_OPEN: probe already sent, blocking")
                return False
            logger.info("Circuit breaker HALF_OPEN: allowing probe signal through")

        # Fetch returns history
        returns = pd.Series(dtype=float)
        if self._returns_provider and callable(self._returns_provider):
            try:
                result = self._returns_provider()
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, np.ndarray) and len(result) > 0:
                    returns = pd.Series(result)
                elif isinstance(result, pd.Series):
                    returns = result
            except Exception as exc:
                logger.error(f"Failed to fetch returns history: {exc}")

        # Run validator chain (fail-fast)
        for validator in self._validators:
            try:
                result = await validator.validate(signal, self._portfolio, returns)

                logger.debug(
                    "Risk validator result",
                    extra={
                        "validator": result.validator_name,
                        "approved": result.approved,
                        "message": result.message,
                        "metric": result.metric_value,
                        "limit": result.limit_value,
                        "signal_id": signal.event_id,
                    },
                )

                if not result.approved:
                    await self._handle_breach(signal, result)
                    return False

            except Exception as exc:
                logger.error(
                    f"Validator {validator.name} raised exception — treating as rejection",
                    extra={"error": str(exc), "signal_id": signal.event_id},
                    exc_info=True,
                )
                return False

        # If we came through HALF_OPEN, confirm recovery
        if self._circuit_breaker.is_half_open:
            self._circuit_breaker.confirm_recovery(operator_id="auto-probe")

        logger.info(
            "Signal approved by risk engine",
            extra={"signal_id": signal.event_id, "symbol": signal.symbol},
        )
        return True

    async def _handle_breach(
        self,
        signal: SignalEvent,
        result: ValidationResult,
    ) -> None:
        """Handle a risk validation failure: emit event, trip breaker if critical."""
        critical_validators = {"DrawdownValidator", "DailyLossValidator"}
        action = "FLATTEN" if result.validator_name in critical_validators else "BLOCK_NEW"

        breach_event = RiskBreachEvent(
            source="risk_engine",
            breach_type=result.validator_name,
            current_value=result.metric_value,
            limit_value=result.limit_value,
            action=action,
            correlation_id=signal.correlation_id,
        )

        try:
            if hasattr(self._event_bus, "publish"):
                await self._event_bus.publish(breach_event)
        except Exception as exc:
            logger.error(f"Failed to publish RiskBreachEvent: {exc}")

        if action == "FLATTEN":
            self._circuit_breaker.trip(reason=result.message)

        logger.warning(
            "Risk breach detected",
            extra={
                "breach_type": result.validator_name,
                "action": action,
                "message": result.message,
                "metric": result.metric_value,
                "limit": result.limit_value,
                "symbol": signal.symbol,
            },
        )

    async def calculate_var(
        self,
        confidence_level: float = 0.99,
        horizon_days: int = 1,
    ) -> float:
        """Portfolio VaR via Historical Simulation."""
        returns = pd.Series(dtype=float)
        if self._returns_provider and callable(self._returns_provider):
            result = self._returns_provider()
            if asyncio.iscoroutine(result):
                returns = await result
            elif isinstance(result, (pd.Series, np.ndarray)):
                returns = pd.Series(result) if isinstance(result, np.ndarray) else result
            else:
                returns = result
        if returns.empty:
            return 0.0
        loop = asyncio.get_event_loop()
        var_1d = await loop.run_in_executor(
            self._executor,
            VaRValidator._compute_historical_var,
            returns,
            confidence_level,
        )
        return var_1d * (horizon_days**0.5)

    async def calculate_cvar(self, confidence_level: float = 0.99) -> float:
        """Conditional VaR (Expected Shortfall)."""
        returns = pd.Series(dtype=float)
        if self._returns_provider and callable(self._returns_provider):
            result = self._returns_provider()
            if asyncio.iscoroutine(result):
                returns = await result
            elif isinstance(result, (pd.Series, np.ndarray)):
                returns = pd.Series(result) if isinstance(result, np.ndarray) else result
            else:
                returns = result
        if returns.empty:
            return 0.0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self._compute_cvar, returns, confidence_level
        )

    @staticmethod
    def _compute_cvar(returns: pd.Series, confidence: float) -> float:
        """Compute CVaR in a thread pool (CPU-bound)."""
        clean = returns.dropna().values
        if len(clean) == 0:
            return 0.0
        var_threshold = np.percentile(clean, (1.0 - confidence) * 100)
        tail_losses = clean[clean <= var_threshold]
        return abs(float(np.mean(tail_losses))) if len(tail_losses) > 0 else abs(var_threshold)

    async def get_current_drawdown(self) -> float:
        """Current drawdown from equity peak using returns series."""
        returns = pd.Series(dtype=float)
        if self._returns_provider and callable(self._returns_provider):
            result = self._returns_provider()
            if asyncio.iscoroutine(result):
                returns = await result
            elif isinstance(result, (pd.Series, np.ndarray)):
                returns = pd.Series(result) if isinstance(result, np.ndarray) else result
            else:
                returns = result
        if returns.empty:
            return 0.0
        cum = (1 + returns).cumprod()
        peak = cum.cummax()
        dd = (cum - peak) / peak
        return abs(float(dd.iloc[-1]))

    def reset_circuit_breaker(self, operator_id: str) -> None:
        """Allow authorised operator to manually reset the circuit breaker."""
        self._circuit_breaker.reset(operator_id=operator_id)

    @property
    def circuit_breaker_state(self) -> str:
        """Current circuit breaker state string."""
        return self._circuit_breaker.state

    def shutdown(self) -> None:
        """Shutdown thread pool (wait=True for clean drain)."""
        self._executor.shutdown(wait=True)
        logger.info("Risk engine thread pool shut down")
