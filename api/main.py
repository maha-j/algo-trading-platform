"""
FastAPI Application — REST API and WebSocket gateway.

Fixes applied (BUG-05, BUG-08, SEC-01, SEC-02, CONTRACT-01):
  - BUG-05: datetime.utcnow() removed throughout.
  - BUG-08: Container retrieved from request.app.state (not get_instance).
  - SEC-01: WebSocket endpoints require ?token= JWT query parameter.
  - SEC-02: ACTIVE_WEBSOCKETS changed from Counter to Gauge.
  - CONTRACT-01: portfolio methods called as sync (no spurious await).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from config.settings import get_settings

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Prometheus Metrics (api/main.py — distinct names from monitoring/metrics.py)
# ─────────────────────────────────────────────────────────────────────────────

REQUEST_COUNT = Counter(
    "api_http_requests_total",
    "Total HTTP requests received by the API gateway",
    ["method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "api_http_request_duration_seconds",
    "HTTP request latency at the API gateway",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
# FIX SEC-02: Gauge (not Counter) — can increment and decrement
ACTIVE_WEBSOCKETS = Gauge(
    "api_websocket_active_connections",
    "Currently active WebSocket connections",
    ["channel"],
)


# ─────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_token(token: str) -> dict:
    """Validate and decode a JWT. Raises HTTPException on failure."""
    try:
        import jwt as pyjwt
        settings = get_settings()
        return pyjwt.decode(
            token,
            settings.api.secret_key.get_secret_value(),
            algorithms=[settings.api.jwt_algorithm],
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ─────────────────────────────────────────────────────────────────────────────
# Application Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan manager.

    Startup:
        1. Initialise DI container
        2. Connect to Redis, PostgreSQL, MT5
        3. Wire event bus subscriptions
        4. Start event consumers

    Shutdown:
        1. Drain event bus
        2. Close all connections
    """
    settings = get_settings()
    logger.info(
        "Trading platform API starting",
        environment = settings.environment,
        version     = settings.version,
    )

    try:
        from infrastructure.container import TradingPlatformContainer
        container = TradingPlatformContainer()
        await container.start()
        app.state.container = container
        logger.info("Platform container started successfully")
    except Exception as exc:
        logger.error("Failed to start platform container", error=str(exc))
        app.state.container = None

    yield

    if hasattr(app.state, "container") and app.state.container:
        await app.state.container.stop()
    logger.info("Trading platform API stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Application Factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Application factory — creates and configures the FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title       = "Institutional Algorithmic Trading Platform",
        description = "Multi-asset algorithmic trading: Forex, Crypto, Stocks, Futures",
        version     = settings.version,
        docs_url    = "/api/docs"        if not settings.is_production() else None,
        redoc_url   = "/api/redoc"       if not settings.is_production() else None,
        openapi_url = "/api/openapi.json" if not settings.is_production() else None,
        lifespan    = lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins      = settings.api.cors_origins,
        allow_credentials  = True,
        allow_methods      = ["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers      = ["*"],
    )

    # ── Request logging + correlation ID ─────────────────────────────────────
    @app.middleware("http")
    async def logging_middleware(request: Request, call_next) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id

        start    = time.monotonic()
        response = await call_next(request)
        elapsed  = time.monotonic() - start

        path   = request.url.path
        method = request.method
        status = response.status_code

        REQUEST_COUNT.labels(method=method, path=path, status_code=str(status)).inc()
        REQUEST_LATENCY.labels(method=method, path=path).observe(elapsed)

        logger.info(
            "HTTP request",
            method         = method,
            path           = path,
            status         = status,
            duration_ms    = round(elapsed * 1000, 2),
            correlation_id = correlation_id,
        )

        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Response-Time"]  = f"{elapsed:.4f}s"
        return response

    # ── System routes ─────────────────────────────────────────────────────────

    @app.get("/health", tags=["System"])
    async def health_check():
        """Basic liveness check — no auth required."""
        return {"status": "ok", "service": "trading-platform-api"}

    @app.get("/health/detailed", tags=["System"])
    async def detailed_health(request: Request):
        """Readiness check — verifies all dependencies."""
        checks: dict[str, str] = {}
        all_ok = True

        container = getattr(request.app.state, "container", None)
        if container is None:
            return JSONResponse(
                status_code = 503,
                content     = {"status": "degraded", "reason": "container not initialised"},
            )

        try:
            if container.event_bus._client:
                await container.event_bus._client.ping()
                checks["redis"] = "ok"
            else:
                checks["redis"] = "not connected"
                all_ok = False
        except Exception as exc:
            checks["redis"] = f"error: {exc}"
            all_ok = False

        try:
            from infrastructure.repositories.db_repositories import ConnectionPool
            pool = await ConnectionPool.get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"error: {exc}"
            all_ok = False

        return JSONResponse(
            status_code = 200 if all_ok else 503,
            content     = {"status": "ok" if all_ok else "degraded", "checks": checks},
        )

    @app.get("/metrics", tags=["System"])
    async def prometheus_metrics():
        """Prometheus metrics endpoint — scraped by Prometheus."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ── API Routers ───────────────────────────────────────────────────────────
    from api.routers import (
        backtest_router,
        market_data_router,
        orders_router,
        portfolio_router,
        risk_router,
        strategies_router,
    )

    app.include_router(market_data_router, prefix="/api/v1/market-data", tags=["Market Data"])
    app.include_router(strategies_router,  prefix="/api/v1/strategies",  tags=["Strategies"])
    app.include_router(orders_router,      prefix="/api/v1/orders",      tags=["Orders"])
    app.include_router(portfolio_router,   prefix="/api/v1/portfolio",   tags=["Portfolio"])
    app.include_router(risk_router,        prefix="/api/v1/risk",        tags=["Risk"])
    app.include_router(backtest_router,    prefix="/api/v1/backtest",    tags=["Backtest"])

    # ── WebSocket Endpoints ───────────────────────────────────────────────────

    @app.websocket("/ws/market-data/{symbol}")
    async def market_data_ws(
        websocket: WebSocket,
        symbol:    str,
        # FIX SEC-01: JWT required as query param for WebSocket auth
        token:     str = Query(..., description="JWT access token"),
    ) -> None:
        """
        Real-time market data feed via WebSocket.

        FIX SEC-01: Requires ?token=<jwt> query parameter.
        """
        # Authenticate before accepting
        try:
            _decode_token(token)
        except HTTPException:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await websocket.accept()
        ACTIVE_WEBSOCKETS.labels(channel="market-data").inc()

        container = websocket.app.state.container
        if not container:
            await websocket.send_json({"error": "Platform not initialised"})
            await websocket.close()
            ACTIVE_WEBSOCKETS.labels(channel="market-data").dec()
            return

        pubsub = (
            container.event_bus._client.pubsub()
            if container.event_bus._client
            else None
        )

        try:
            if pubsub:
                await pubsub.subscribe(f"pubsub:{symbol}")

            await websocket.send_json({"type": "SUBSCRIBED", "symbol": symbol})

            while True:
                try:
                    if pubsub:
                        message = await asyncio.wait_for(
                            pubsub.get_message(ignore_subscribe_messages=True),
                            timeout=1.0,
                        )
                        if message and message.get("type") == "message":
                            data = message["data"]
                            await websocket.send_text(
                                data.decode() if isinstance(data, bytes) else data
                            )
                    else:
                        await asyncio.sleep(1.0)
                        await websocket.send_json({"type": "HEARTBEAT", "symbol": symbol})

                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "PING"})
                except Exception as exc:
                    logger.error(f"WebSocket error for {symbol}: {exc}")
                    break

        except Exception as exc:
            logger.error(f"WebSocket connection error: {exc}")
        finally:
            if pubsub:
                await pubsub.unsubscribe(f"pubsub:{symbol}")
            await websocket.close()
            # FIX SEC-02: dec() works because we use Gauge not Counter
            ACTIVE_WEBSOCKETS.labels(channel="market-data").dec()
            logger.info(f"WebSocket market-data disconnected for {symbol}")

    @app.websocket("/ws/portfolio")
    async def portfolio_ws(
        websocket: WebSocket,
        # FIX SEC-01: JWT required for portfolio WebSocket
        token:     str = Query(..., description="JWT access token"),
    ) -> None:
        """
        Real-time portfolio updates via WebSocket.
        FIX SEC-01: Requires ?token=<jwt> query parameter.
        FIX CONTRACT-01: portfolio methods called synchronously (no await).
        """
        try:
            _decode_token(token)
        except HTTPException:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await websocket.accept()
        ACTIVE_WEBSOCKETS.labels(channel="portfolio").inc()

        container = websocket.app.state.container
        if not container:
            await websocket.send_json({"error": "Platform not initialised"})
            await websocket.close()
            ACTIVE_WEBSOCKETS.labels(channel="portfolio").dec()
            return

        try:
            await websocket.send_json({"type": "SUBSCRIBED", "channel": "portfolio"})

            while True:
                try:
                    # FIX CONTRACT-01: get_positions/get_equity are sync — no await
                    summary = container.portfolio_engine.get_summary()

                    await websocket.send_json({
                        "type":            "PORTFOLIO_UPDATE",
                        "equity":          summary["equity"],
                        "realised_pnl":    summary["realised_pnl"],
                        "unrealised_pnl":  summary["unrealised_pnl"],
                        "drawdown_pct":    summary["drawdown_pct"],
                        "open_positions":  summary["open_positions"],
                        "positions":       summary["positions"],
                    })
                except Exception as exc:
                    logger.error(f"Portfolio WS update error: {exc}")

                await asyncio.sleep(1.0)

        except Exception as exc:
            logger.error(f"Portfolio WebSocket error: {exc}")
        finally:
            await websocket.close()
            ACTIVE_WEBSOCKETS.labels(channel="portfolio").dec()
            logger.info("Portfolio WebSocket disconnected")

    return app


# Module-level app instance for uvicorn
app = create_app()
