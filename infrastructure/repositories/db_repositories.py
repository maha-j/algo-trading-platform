"""
Database Repositories — asyncpg-based TimescaleDB persistence layer.

Fixes applied (DEPLOY-05):
  - DEPLOY-05: settings.database.url → settings.database.asyncpg_dsn
               (the .url property didn't exist; asyncpg_dsn is the correct
                raw DSN without the SQLAlchemy driver prefix).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

from config.settings import get_settings
from core.domain.events import BarEvent, FillEvent, OrderEvent

logger = logging.getLogger(__name__)


class ConnectionPool:
    """Thin wrapper around asyncpg connection pool with lazy initialisation."""

    _pool: asyncpg.Pool | None = None

    @classmethod
    async def get_pool(cls) -> asyncpg.Pool:
        if cls._pool is None:
            settings = get_settings()
            cls._pool = await asyncpg.create_pool(
                # FIX DEPLOY-05: was settings.database.url (does not exist)
                dsn=settings.database.asyncpg_dsn,
                min_size=settings.database.pool_min,
                max_size=settings.database.pool_max,
                timeout=settings.database.pool_timeout,
                command_timeout=60,
            )
            logger.info(
                "asyncpg connection pool created",
                extra={
                    "host": settings.database.host,
                    "database": settings.database.name,
                    "pool_min": settings.database.pool_min,
                    "pool_max": settings.database.pool_max,
                },
            )
        return cls._pool

    @classmethod
    async def close(cls) -> None:
        if cls._pool:
            await cls._pool.close()
            cls._pool = None
            logger.info("asyncpg connection pool closed")


class OrderRepository:
    """Persist and query OrderEvents."""

    async def save_order(self, order: OrderEvent) -> None:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (
                    order_id, symbol, order_type, side, quantity,
                    limit_price, stop_price, strategy_id, algorithm,
                    risk_approved, source, correlation_id, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (order_id) DO NOTHING
                """,
                order.order_id,
                order.symbol,
                order.order_type,
                order.side,
                float(order.quantity),
                float(order.limit_price) if order.limit_price else None,
                float(order.stop_price) if order.stop_price else None,
                order.strategy_id,
                order.algorithm,
                order.risk_approved,
                order.source,
                order.correlation_id,
                order.timestamp,
            )

    async def get_open_orders(self) -> list[dict]:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM orders
                WHERE status NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED')
                ORDER BY created_at DESC
                """
            )
        return [dict(r) for r in rows]


class FillRepository:
    """Persist FillEvents and query P&L."""

    async def save_fill(self, fill: FillEvent) -> None:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fills (
                    fill_id, order_id, symbol, side,
                    quantity, fill_price, commission, slippage,
                    source, correlation_id, filled_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (fill_id) DO NOTHING
                """,
                fill.event_id,
                fill.order_id,
                fill.symbol,
                fill.side,
                float(fill.quantity),
                float(fill.fill_price),
                float(fill.commission),
                float(fill.slippage),
                fill.source,
                fill.correlation_id,
                fill.timestamp,
            )

    async def sum_pnl_since(self, since: datetime) -> Decimal:
        """
        Sum net P&L for fills since a given timestamp.
        Used by PortfolioEngine.get_daily_pnl() (BUG-04 fix).
        """
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT COALESCE(SUM(
                    CASE side
                        WHEN 'SELL' THEN  quantity * fill_price - commission
                        WHEN 'BUY'  THEN -(quantity * fill_price + commission)
                        ELSE 0
                    END
                ), 0.0)
                FROM fills
                WHERE filled_at >= $1
                """,
                since,
            )
        return Decimal(str(result))

    async def get_fills_for_symbol(self, symbol: str, limit: int = 100) -> list[dict]:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM fills WHERE symbol=$1 ORDER BY filled_at DESC LIMIT $2",
                symbol,
                limit,
            )
        return [dict(r) for r in rows]


class BarRepository:
    """Persist and query OHLCV bar data (TimescaleDB hypertable)."""

    async def save_bar(self, bar: BarEvent) -> None:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bars (time, symbol, timeframe, open, high, low, close, volume)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (time, symbol, timeframe) DO UPDATE
                    SET open=$4, high=$5, low=$6, close=$7, volume=$8
                """,
                bar.timestamp,
                bar.symbol,
                bar.timeframe,
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
            )

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> list[dict]:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM bars
                WHERE symbol=$1 AND timeframe=$2
                ORDER BY time DESC
                LIMIT $3
                """,
                symbol,
                timeframe,
                limit,
            )
        return [dict(r) for r in rows]


class PortfolioSnapshotRepository:
    """Persist portfolio equity snapshots for drawdown analysis."""

    async def save_snapshot(
        self,
        equity: float,
        cash: float,
        realised_pnl: float,
        unrealised_pnl: float,
        drawdown_pct: float,
        open_positions: int,
    ) -> None:
        pool = await ConnectionPool.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO portfolio_snapshots
                    (time, equity, cash, realised_pnl, unrealised_pnl,
                     drawdown_pct, open_positions)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                datetime.now(timezone.utc),
                equity,
                cash,
                realised_pnl,
                unrealised_pnl,
                drawdown_pct,
                open_positions,
            )
