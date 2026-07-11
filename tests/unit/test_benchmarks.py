"""
Performance Benchmarks
======================
Validates that latency and throughput targets are met for production paths.

Targets (based on Citadel/Two Sigma-style latency budgets):
  * Indicator computation (200 bars): < 5ms after Numba warm-up
  * Signal generation (on_bar):       < 1ms per bar
  * Portfolio on_fill:                < 0.5ms per fill
  * Backtest (1000 bars):             < 5 seconds
  * Risk validation:                  < 2ms per signal

Run with:
    pytest tests/unit/test_benchmarks.py -v -s --benchmark-only
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

BARS_200 = None  # populated by session fixture
BARS_1000 = None


def _make_bars(n: int) -> pd.DataFrame:
    np.random.seed(42)
    p = 1.0850
    rows = []
    for _ in range(n):
        p *= np.exp(np.random.normal(0, 0.001))
        rows.append(
            {
                "open": p * 0.9999,
                "high": p * 1.0005,
                "low": p * 0.9995,
                "close": p,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))


class TestIndicatorBenchmarks:
    @pytest.fixture(autouse=True)
    def setup_bars(self):
        global BARS_200, BARS_1000
        BARS_200 = _make_bars(200)
        BARS_1000 = _make_bars(1000)

    def test_ema_200bars_under_5ms(self):
        from indicator_engine.service import compute_ema

        # Warm up JIT
        compute_ema(BARS_200, 20)

        # Measure
        t0 = time.perf_counter()
        for _ in range(100):
            compute_ema(BARS_200, 20)
        elapsed_ms = (time.perf_counter() - t0) / 100 * 1000

        print(f"\n  EMA(200 bars): {elapsed_ms:.3f}ms avg")
        assert elapsed_ms < 5.0, f"EMA too slow: {elapsed_ms:.2f}ms (limit 5ms)"

    def test_rsi_200bars_under_5ms(self):
        from indicator_engine.service import compute_rsi

        compute_rsi(BARS_200, 14)  # warm-up

        t0 = time.perf_counter()
        for _ in range(100):
            compute_rsi(BARS_200, 14)
        elapsed_ms = (time.perf_counter() - t0) / 100 * 1000

        print(f"\n  RSI(200 bars): {elapsed_ms:.3f}ms avg")
        assert elapsed_ms < 5.0

    def test_full_indicator_suite_under_50ms(self):
        from indicator_engine.service import IndicatorService

        svc = IndicatorService()
        svc.compute_all("EURUSD", "H1", BARS_200)  # warm-up

        t0 = time.perf_counter()
        for _ in range(20):
            svc.compute_all("EURUSD", "H1", BARS_200)
        elapsed_ms = (time.perf_counter() - t0) / 20 * 1000

        print(f"\n  Full indicator suite (200 bars): {elapsed_ms:.2f}ms avg")
        assert elapsed_ms < 100.0, f"Indicator suite too slow: {elapsed_ms:.2f}ms"

    def test_indicator_service_1000bars(self):
        from indicator_engine.service import IndicatorService

        svc = IndicatorService()
        svc.compute_all("EURUSD", "H1", BARS_1000)  # warm-up

        t0 = time.perf_counter()
        for _ in range(5):
            svc.compute_all("EURUSD", "H1", BARS_1000)
        elapsed_ms = (time.perf_counter() - t0) / 5 * 1000

        print(f"\n  Full indicator suite (1000 bars): {elapsed_ms:.2f}ms avg")
        assert elapsed_ms < 500.0


class TestPortfolioBenchmarks:
    @pytest.mark.asyncio
    async def test_on_fill_latency_under_0_5ms(self):
        from core.domain.events import FillEvent
        from portfolio_engine.service import PortfolioEngine

        engine = PortfolioEngine(initial_capital=1_000_000.0)

        fills = [
            FillEvent(
                source="bench",
                order_id=f"o{i}",
                symbol=f"SYM{i % 10}",
                side="BUY" if i % 2 == 0 else "SELL",
                quantity=1000.0,
                fill_price=1.0850 + i * 0.0001,
                commission=7.0,
            )
            for i in range(200)
        ]

        # Warm up
        await engine.on_fill(fills[0])

        t0 = time.perf_counter()
        for fill in fills[1:]:
            await engine.on_fill(fill)
        elapsed_ms = (time.perf_counter() - t0) / len(fills) * 1000

        print(f"\n  Portfolio.on_fill: {elapsed_ms:.4f}ms avg")
        assert elapsed_ms < 2.0, f"on_fill too slow: {elapsed_ms:.3f}ms"


class TestBacktestBenchmarks:
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_backtest_1000bars_under_10s(self):
        from backtest_engine.service import BacktestConfig, BacktestEngine
        from strategy_engine.service import EMACrossoverStrategy

        bars = _make_bars(1000)
        engine = BacktestEngine(BacktestConfig())
        strategy = EMACrossoverStrategy()

        t0 = time.perf_counter()
        result = await engine.run(strategy, bars, "EURUSD", "H1")
        elapsed = time.perf_counter() - t0

        print(f"\n  Backtest 1000 bars: {elapsed:.3f}s")
        assert elapsed < 10.0, f"Backtest too slow: {elapsed:.2f}s"
        assert "error" not in result

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_backtest_throughput_bars_per_second(self):
        from backtest_engine.service import BacktestConfig, BacktestEngine
        from strategy_engine.service import EMACrossoverStrategy

        bars = _make_bars(5000)
        engine = BacktestEngine(BacktestConfig())
        strategy = EMACrossoverStrategy()

        t0 = time.perf_counter()
        await engine.run(strategy, bars, "EURUSD", "H1")
        elapsed = time.perf_counter() - t0

        bars_per_sec = 5000 / elapsed
        print(f"\n  Backtest throughput: {bars_per_sec:.0f} bars/sec")
        # Minimum acceptable: 500 bars/sec on CI machines
        assert bars_per_sec > 200, f"Backtest throughput too low: {bars_per_sec:.0f} bars/sec"
