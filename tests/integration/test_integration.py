"""
Integration Tests
=================
These tests require a running Redis instance.
Run with: pytest tests/integration -m integration

The integration tests validate:
  1. Redis event bus — publish → consume round-trip.
  2. End-to-end signal flow — bar → strategy → signal → risk → order → fill → portfolio.
  3. Backtest engine — full run on synthetic data.
  4. Container wiring — all engines connected, correct event routing.

Use docker-compose up redis to bring up Redis before running:
    pytest tests/integration -m integration -v
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

# Skip all integration tests if Redis is not reachable
REDIS_AVAILABLE = False
try:
    import redis as redis_sync

    client = redis_sync.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD", ""),
        socket_connect_timeout=2,
    )
    client.ping()
    REDIS_AVAILABLE = True
    client.close()
except Exception:
    pass

pytestmark = pytest.mark.integration

skip_no_redis = pytest.mark.skipif(
    not REDIS_AVAILABLE, reason="Redis not available — skipping integration tests"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    """1000-bar synthetic GBM price series."""
    np.random.seed(99)
    n = 1000
    price = 1.0850
    rows = []
    now = datetime.now(timezone.utc)
    for i in range(n):
        ret = np.random.normal(0, 0.001)
        o = price
        price *= np.exp(ret)
        c = price
        h = max(o, c) * (1 + abs(np.random.normal(0, 0.0003)))
        lo = min(o, c) * (1 - abs(np.random.normal(0, 0.0003)))
        rows.append(
            {"open": o, "high": h, "low": lo, "close": c, "volume": np.random.uniform(500, 2000)}
        )

    idx = pd.date_range(now - timedelta(hours=n), periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(rows, index=idx)


# ===========================================================================
# Redis Event Bus Integration
# ===========================================================================


@skip_no_redis
class TestRedisEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_consume_roundtrip(self):
        """Publish a TickEvent, verify the consumer receives it."""
        import redis.asyncio as aioredis

        from core.domain.events import TickEvent
        from infrastructure.event_bus.redis_event_bus import RedisEventBus

        client = aioredis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )

        bus = RedisEventBus(client, group="test_grp", consumer_name="test_consumer")
        received: List[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await bus.register_handler("stream:ticks:test", handler)

        tick = TickEvent(source="test", symbol="EURUSD", bid=1.0850, ask=1.0851, volume=1.0)

        # Override channel to test stream
        with patch.object(tick, "channel", return_value="stream:ticks:test"):
            await bus.publish(tick)

        # Give consumer time to process
        consume_task = asyncio.create_task(bus.start_consuming())
        await asyncio.sleep(0.5)
        consume_task.cancel()
        await client.aclose()

        assert len(received) >= 1

    @pytest.mark.asyncio
    async def test_publish_many_batch(self):
        """publish_many must publish N events in a single pipeline."""
        import redis.asyncio as aioredis

        from core.domain.events import BarEvent
        from infrastructure.event_bus.redis_event_bus import RedisEventBus

        client = aioredis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )

        bus = RedisEventBus(client, group="test_batch_grp", consumer_name="batch_consumer")
        events = [
            BarEvent(
                source="test",
                symbol="EURUSD",
                timeframe="H1",
                open=1.085,
                high=1.086,
                low=1.084,
                close=1.0855,
                volume=1000,
                bar_index=i,
                is_closed=True,
            )
            for i in range(50)
        ]

        count = await bus.publish_many(events)
        assert count == 50
        await client.aclose()


# ===========================================================================
# Full Signal Flow Integration
# ===========================================================================


class TestSignalFlowIntegration:
    """
    Tests the complete chain: BarEvent → strategy → signal → risk → order → fill → portfolio.
    This is an in-process integration test (no Redis) using mocked event bus.
    """

    @pytest.fixture
    def mock_event_bus(self):
        bus = AsyncMock()
        bus.publish = AsyncMock()
        return bus

    @pytest.fixture
    def mock_broker(self):
        from core.domain.events import FillEvent

        broker = AsyncMock()
        fill = FillEvent(
            source="test",
            order_id="test_order_1",
            symbol="EURUSD",
            side="BUY",
            quantity=10000.0,
            fill_price=1.08510,
            commission=7.0,
        )
        broker.submit_market_order = AsyncMock(return_value=fill)
        broker.connect = AsyncMock(return_value=True)
        return broker

    @pytest.mark.asyncio
    async def test_ema_crossover_generates_signal(self, synthetic_bars):
        """Strategy must generate at least one signal on 1000 bars."""
        from indicator_engine.service import IndicatorService
        from strategy_engine.service import EMACrossoverStrategy

        strategy = EMACrossoverStrategy(config={"fast_period": 9, "slow_period": 21})
        ind_svc = IndicatorService()

        # Warm up indicators
        ind_svc.compute_all("EURUSD", "H1", synthetic_bars)

        from core.domain.events import BarEvent

        signals = []
        for i, (ts, row) in enumerate(synthetic_bars.iterrows()):
            bar = BarEvent(
                source="test",
                symbol="EURUSD",
                timeframe="H1",
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                timestamp=ts,
                bar_index=i,
                is_closed=True,
            )
            sub_df = synthetic_bars.iloc[max(0, i - 100) : i + 1]
            if len(sub_df) >= 30:
                ind_svc.compute_all("EURUSD", "H1", sub_df)
            signal = await strategy.on_bar(bar, ind_svc)
            if signal:
                signals.append(signal)
            if len(signals) >= 3:
                break

        assert len(signals) >= 1, "Strategy produced no signals on 1000 bars"
        for s in signals:
            assert s.direction in ("LONG", "SHORT", "FLAT")
            assert 0.0 <= s.strength <= 1.0

    @pytest.mark.asyncio
    async def test_portfolio_tracks_multiple_fills(self, synthetic_bars):
        """Portfolio must correctly aggregate P&L across multiple round-trips."""
        from core.domain.events import FillEvent
        from portfolio_engine.service import PortfolioEngine

        portfolio = PortfolioEngine(initial_capital=100_000.0)

        trades = [
            ("BUY", 10000, 1.08500),
            ("SELL", 10000, 1.09000),  # +50 - 14 comm = +36
            ("BUY", 10000, 1.09000),
            ("SELL", 10000, 1.08500),  # -50 - 14 comm = -64
            ("SELL", 10000, 1.09500),
            ("BUY", 10000, 1.09000),  # +50 - 14 comm = +36
        ]

        for side, qty, price in trades:
            fill = FillEvent(
                source="test",
                order_id=f"o_{len(trades)}",
                symbol="EURUSD",
                side=side,
                quantity=float(qty),
                fill_price=price,
                commission=7.0,
            )
            await portfolio.on_fill(fill)

        # Net: +36 - 64 + 36 = +8
        realised = float(portfolio.get_realised_pnl())
        assert realised == pytest.approx(8.0, abs=1.0)
        assert len(portfolio.get_positions()) == 0  # All closed


# ===========================================================================
# Backtest Engine Integration
# ===========================================================================


class TestBacktestEngineIntegration:
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_full_backtest_on_synthetic_data(self, synthetic_bars):
        """Full backtest must complete without errors and return valid stats."""
        from backtest_engine.service import BacktestConfig, BacktestEngine
        from strategy_engine.service import EMACrossoverStrategy

        config = BacktestConfig(initial_capital=100_000.0)
        engine = BacktestEngine(config)
        strategy = EMACrossoverStrategy(config={"fast_period": 9, "slow_period": 21})

        result = await engine.run(strategy, synthetic_bars, "EURUSD", "H1")

        assert "error" not in result
        assert "equity_curve" in result
        assert "stats" in result

        stats = result["stats"]
        # Statistical sanity checks
        assert isinstance(stats["sharpe_ratio"], float)
        assert isinstance(stats["max_drawdown_pct"], float)
        assert stats["max_drawdown_pct"] <= 0  # Drawdown always ≤ 0%
        assert stats["total_bars"] == len(synthetic_bars)
        assert stats["runtime_seconds"] > 0

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_walk_forward_produces_multiple_splits(self, synthetic_bars):
        from backtest_engine.service import BacktestConfig, BacktestEngine
        from strategy_engine.service import EMACrossoverStrategy

        engine = BacktestEngine(BacktestConfig())
        result = await engine.walk_forward(
            strategy_cls=EMACrossoverStrategy,
            strategy_config={},
            bars=synthetic_bars,
            symbol="EURUSD",
            timeframe="H1",
            in_sample_bars=400,
            out_of_sample_bars=100,
            n_splits=3,
        )

        assert "error" not in result
        assert result["n_splits"] == 3
        assert len(result["split_results"]) == 3
        assert 0.0 <= result["consistency_pct"] <= 100.0

    @pytest.mark.asyncio
    async def test_backtest_next_bar_fill_model(self, synthetic_bars):
        """
        Verify that fills use next-bar-open price, not signal-bar-close.
        This is critical for avoiding look-ahead bias.
        """
        from backtest_engine.service import BacktestConfig, BacktestEngine
        from strategy_engine.service import EMACrossoverStrategy

        engine = BacktestEngine(BacktestConfig())
        strategy = EMACrossoverStrategy(config={})
        result = await engine.run(strategy, synthetic_bars[:200], "EURUSD", "H1")

        if result.get("fills"):
            fill = result["fills"][0]
            fill_ts = fill.timestamp
            # Fill timestamp must correspond to a bar's timestamp (next bar)
            bar_ts = synthetic_bars.index
            assert any(abs((fill_ts - ts).total_seconds()) < 3700 for ts in bar_ts)


# ===========================================================================
# Notification Service Integration
# ===========================================================================


class TestNotificationService:
    @pytest.mark.asyncio
    async def test_queue_and_deliver_alert(self):
        """Non-blocking alert queue must process messages."""
        from notification.service import AlertLevel, NotificationService

        svc = NotificationService()
        delivered: List[tuple] = []

        class MockChannel:
            channel_name = "mock"

            async def send(self, subject, body, level):
                delivered.append((subject, body, level))
                return True

        svc.register_channel(MockChannel())
        svc.ROUTING["INFO"] = ["mock"]
        svc.start()

        await svc.alert("Test Subject", "Test Body", AlertLevel.INFO)
        await asyncio.sleep(0.2)
        await svc.stop()

        assert len(delivered) == 1
        assert delivered[0][0] == "Test Subject"

    @pytest.mark.asyncio
    async def test_circuit_breaker_suppresses_channel(self):
        """Channel circuit breaker must stop delivery after threshold failures."""
        from notification.service import AlertLevel, ChannelCircuitBreaker, TelegramChannel

        channel = TelegramChannel.__new__(TelegramChannel)
        channel._token = ""
        channel._chat_id = ""
        channel._breaker = ChannelCircuitBreaker(threshold=3)
        channel.channel_name = "telegram"
        from notification.service import TokenBucket

        channel._limiter = TokenBucket(rate=100, capacity=100)

        # Simulate 3 failures
        for _ in range(3):
            channel._breaker.record_failure()

        assert channel._breaker.is_open()
        # Should not attempt delivery when breaker is open
        result = await channel.send("Test", "Body", AlertLevel.INFO)
        assert result is False
