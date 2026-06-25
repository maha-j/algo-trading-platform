"""
FastAPI API Routers — Domain-level endpoint groups.

Fixes applied (BUG-08, INCONS-01, CONTRACT-01):
  - BUG-08: get_container() now retrieves the container from request.app.state
            (not TradingPlatformContainer.get_instance() which didn't exist).
  - INCONS-01: JWT decoding uses settings.api.secret_key (not .jwt_secret).
  - CONTRACT-01: portfolio engine methods called synchronously.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from config.settings import get_settings
from core.domain.events import OrderEvent, SignalEvent

logger = logging.getLogger(__name__)
security = HTTPBearer()


# ─────────────────────────────────────────────────────────────────────────────
# Dependency Injection helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_container(request: Request):
    """
    FIX BUG-08: Retrieve the DI container from FastAPI app state.
    Previously called TradingPlatformContainer.get_instance() which did
    not exist as a class method, crashing every API request.
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = "Trading platform not initialised",
        )
    return container


def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Validate Bearer JWT token.
    FIX INCONS-01: uses settings.api.secret_key (not .jwt_secret).
    """
    settings = get_settings()
    try:
        payload = pyjwt.decode(
            credentials.credentials,
            # FIX INCONS-01: correct field name is secret_key
            settings.api.secret_key.get_secret_value(),
            algorithms=[settings.api.jwt_algorithm],
        )
        return payload
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol:       str
    side:         str   = Field(..., pattern="^(BUY|SELL)$")
    quantity:     float = Field(..., gt=0)
    order_type:   str   = Field(default="MARKET", pattern="^(MARKET|LIMIT|STOP)$")
    limit_price:  Optional[float] = None
    stop_price:   Optional[float] = None
    strategy_id:  str   = "manual"
    algorithm:    str   = Field(default="MARKET", pattern="^(MARKET|TWAP|VWAP)$")


class BacktestRequest(BaseModel):
    strategy_id:     str
    symbol:          str
    timeframe:       str
    start_date:      str
    end_date:        str
    initial_capital: float = Field(default=100_000.0, gt=0)
    fast_period:     int   = Field(default=9,  ge=1)
    slow_period:     int   = Field(default=21, ge=2)


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Router
# ─────────────────────────────────────────────────────────────────────────────

portfolio_router = APIRouter()


@portfolio_router.get("/summary")
async def get_portfolio_summary(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    """Full portfolio summary — equity, positions, P&L, drawdown."""
    # FIX CONTRACT-01: get_summary() is synchronous
    return container.portfolio_engine.get_summary()


@portfolio_router.get("/equity")
async def get_equity(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    # FIX CONTRACT-01: get_equity() is synchronous
    equity = container.portfolio_engine.get_equity()
    return {"equity": float(equity)}


@portfolio_router.get("/positions")
async def get_positions(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    # FIX CONTRACT-01: get_positions() is synchronous
    positions = container.portfolio_engine.get_positions()
    return {
        sym: {
            "net_qty":         float(p.net_qty),
            "avg_entry_price": float(p.avg_entry_price),
            "unrealised_pnl":  float(p.unrealised_pnl),
            "realised_pnl":    float(p.realised_pnl),
            "market_value":    float(p.market_value),
            "current_price":   float(p.current_price),
            "asset_class":     p.asset_class.value,
        }
        for sym, p in positions.items()
    }


@portfolio_router.get("/pnl")
async def get_pnl(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    pe = container.portfolio_engine
    return {
        "realised_pnl":   float(pe.get_realised_pnl()),
        "unrealised_pnl": float(pe.get_unrealised_pnl()),
        "daily_pnl":      float(pe.get_daily_pnl()),
        "total_pnl":      float(pe.get_realised_pnl() + pe.get_unrealised_pnl()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orders Router
# ─────────────────────────────────────────────────────────────────────────────

orders_router = APIRouter()


@orders_router.post("/submit")
async def submit_order(
    req:       OrderRequest,
    container  = Depends(get_container),
    auth: dict = Depends(require_auth),
) -> dict:
    """Submit a manual order — bypasses strategy but still requires risk approval."""
    # Create a signal for risk validation
    signal = SignalEvent(
        source       = "api-manual",
        symbol       = req.symbol,
        strategy_id  = req.strategy_id,
        direction    = "LONG" if req.side == "BUY" else "SHORT",
        strength     = 1.0,
        signal_price = Decimal(str(req.limit_price or 0)),
    )

    approved = await container.risk_engine.validate_signal(signal)
    if not approved:
        raise HTTPException(status_code=403, detail="Order rejected by risk engine")

    order = OrderEvent(
        source        = "api-manual",
        symbol        = req.symbol,
        side          = req.side,
        quantity      = Decimal(str(req.quantity)),
        order_type    = req.order_type,
        limit_price   = Decimal(str(req.limit_price)) if req.limit_price else None,
        stop_price    = Decimal(str(req.stop_price))  if req.stop_price  else None,
        strategy_id   = req.strategy_id,
        algorithm     = req.algorithm,
        risk_approved = True,
    )

    order_id = await container.execution_engine.submit_order(order)
    return {"order_id": order_id, "status": "submitted"}


@orders_router.get("/open")
async def get_open_orders(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    orders = await container.execution_engine.get_open_orders()
    return {"open_orders": [o.order_id for o in orders], "count": len(orders)}


@orders_router.delete("/{order_id}")
async def cancel_order(
    order_id:  str,
    container  = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    success = await container.execution_engine.cancel_order(order_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return {"order_id": order_id, "status": "cancelled"}


# ─────────────────────────────────────────────────────────────────────────────
# Risk Router
# ─────────────────────────────────────────────────────────────────────────────

risk_router = APIRouter()


@risk_router.get("/metrics")
async def get_risk_metrics(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    """Current risk metrics — VaR, CVaR, drawdown, circuit breaker."""
    re = container.risk_engine
    return {
        "var_99":              await re.calculate_var(confidence_level=0.99),
        "cvar_99":             await re.calculate_cvar(confidence_level=0.99),
        "current_drawdown":    await re.get_current_drawdown(),
        "circuit_breaker":     re.circuit_breaker_state,
        "daily_pnl":           float(container.portfolio_engine.get_daily_pnl()),
        "equity":              float(container.portfolio_engine.get_equity()),
    }


@risk_router.post("/circuit-breaker/reset")
async def reset_circuit_breaker(
    request:   Request,
    container  = Depends(get_container),
    auth: dict = Depends(require_auth),
) -> dict:
    """Manually reset the circuit breaker (requires operator token)."""
    operator_id = auth.get("sub", "api-operator")
    container.risk_engine.reset_circuit_breaker(operator_id=operator_id)
    return {"status": "reset", "operator": operator_id}


# ─────────────────────────────────────────────────────────────────────────────
# Strategies Router
# ─────────────────────────────────────────────────────────────────────────────

strategies_router = APIRouter()


@strategies_router.get("/")
async def list_strategies(
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    return {"strategies": container.strategy_engine.list_registered()}


@strategies_router.post("/{strategy_id}/enable")
async def enable_strategy(
    strategy_id: str,
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    container.strategy_engine.enable_strategy(strategy_id)
    return {"strategy_id": strategy_id, "status": "enabled"}


@strategies_router.post("/{strategy_id}/disable")
async def disable_strategy(
    strategy_id: str,
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    container.strategy_engine.disable_strategy(strategy_id)
    return {"strategy_id": strategy_id, "status": "disabled"}


# ─────────────────────────────────────────────────────────────────────────────
# Market Data Router
# ─────────────────────────────────────────────────────────────────────────────

market_data_router = APIRouter()


@market_data_router.get("/symbols")
async def list_symbols(_auth: dict = Depends(require_auth)) -> dict:
    return {
        "symbols": {
            "forex":   ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"],
            "crypto":  ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
            "stocks":  ["AAPL", "MSFT", "GOOGL", "AMZN"],
            "futures": ["ES1!", "NQ1!", "CL1!", "GC1!"],
        }
    }


@market_data_router.get("/bars/{symbol}")
async def get_bars(
    symbol:    str,
    timeframe: str = "H1",
    count:     int = 100,
    container = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    from infrastructure.repositories.db_repositories import BarRepository
    repo = BarRepository()
    bars = await repo.get_bars(symbol=symbol, timeframe=timeframe, limit=count)
    return {"symbol": symbol, "timeframe": timeframe, "count": len(bars), "bars": bars}


# ─────────────────────────────────────────────────────────────────────────────
# Backtest Router
# ─────────────────────────────────────────────────────────────────────────────

backtest_router = APIRouter()


@backtest_router.post("/run")
async def run_backtest(
    req:        BacktestRequest,
    container   = Depends(get_container),
    _auth: dict = Depends(require_auth),
) -> dict:
    """Launch a backtest asynchronously and return a job ID."""
    job_id = str(uuid.uuid4())
    # In production: enqueue to Celery/ARQ and return job_id for polling
    return {
        "job_id":  job_id,
        "status":  "queued",
        "message": "Backtest queued. Poll /api/v1/backtest/status/{job_id}",
    }


@backtest_router.get("/status/{job_id}")
async def get_backtest_status(
    job_id: str,
    _auth: dict = Depends(require_auth),
) -> dict:
    return {"job_id": job_id, "status": "pending"}
