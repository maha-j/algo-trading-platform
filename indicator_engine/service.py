"""
Indicator Engine
================
Provides a registry of technical indicators backed by Numba JIT-compiled
kernels for near-C performance on large bar arrays.

Design decisions
----------------
* All computation functions are pure (no side effects, no state).
* Numba @njit kernels operate on raw NumPy arrays for maximum speed.
  First call triggers JIT compilation; subsequent calls are near-C speed.
* `IndicatorService` is a thin registry/cache layer. It stores the last
  computed Series per (symbol, timeframe, indicator) so the Strategy Engine
  can call get() without re-computing on every bar.
* VWAP is session-based (resets at market open) and is NOT Numba-compiled
  because it requires a rolling Pandas GroupBy by session date.
* All indicators return a pandas Series with the same DatetimeIndex as the
  input DataFrame so they can be directly aligned with bar data.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from numba import njit
    NUMBA_AVAILABLE = True
except ImportError:
    # Fallback: identity decorator so code runs without Numba installed
    def njit(*args, **kwargs):
        def wrapper(fn):
            return fn
        return wrapper if args and callable(args[0]) else wrapper
    NUMBA_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numba-JIT kernels (pure NumPy arrays, no Pandas inside)
# ---------------------------------------------------------------------------

@njit(cache=True)
def _ema_kernel(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average — Wilder's smoothing (α = 2/(n+1))."""
    alpha = 2.0 / (period + 1)
    out = np.empty(len(values), dtype=np.float64)
    out[:] = np.nan
    # Seed with SMA over first `period` valid values
    start = 0
    while start < len(values) and np.isnan(values[start]):
        start += 1
    if start + period > len(values):
        return out
    seed = np.mean(values[start:start + period])
    out[start + period - 1] = seed
    for i in range(start + period, len(values)):
        if np.isnan(values[i]):
            out[i] = out[i - 1]
        else:
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


@njit(cache=True)
def _rsi_kernel(close: np.ndarray, period: int) -> np.ndarray:
    """Relative Strength Index using Wilder's smoothing."""
    n = len(close)
    out = np.empty(n, dtype=np.float64)
    out[:] = np.nan
    if n < period + 1:
        return out

    gains = np.empty(n - 1, dtype=np.float64)
    losses = np.empty(n - 1, dtype=np.float64)
    for i in range(n - 1):
        diff = close[i + 1] - close[i]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0.0 else 1e9
        out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


@njit(cache=True)
def _atr_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average True Range — Wilder smoothing."""
    n = len(close)
    out = np.empty(n, dtype=np.float64)
    out[:] = np.nan

    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)

    if n < period:
        return out

    out[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


@njit(cache=True)
def _sma_kernel(values: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average."""
    n = len(values)
    out = np.empty(n, dtype=np.float64)
    out[:] = np.nan
    for i in range(period - 1, n):
        out[i] = np.mean(values[i - period + 1: i + 1])
    return out


@njit(cache=True)
def _stddev_kernel(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling standard deviation."""
    n = len(values)
    out = np.empty(n, dtype=np.float64)
    out[:] = np.nan
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        mean = np.mean(window)
        variance = np.mean((window - mean) ** 2)
        out[i] = np.sqrt(variance)
    return out


@njit(cache=True)
def _stochastic_kernel(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, k_period: int, d_period: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Stochastic Oscillator %K and %D."""
    n = len(close)
    k = np.empty(n, dtype=np.float64)
    k[:] = np.nan
    for i in range(k_period - 1, n):
        hh = np.max(high[i - k_period + 1: i + 1])
        ll = np.min(low[i - k_period + 1: i + 1])
        rng = hh - ll
        k[i] = ((close[i] - ll) / rng * 100.0) if rng != 0.0 else 50.0

    d = _sma_kernel(k, d_period)
    return k, d


# ---------------------------------------------------------------------------
# High-level indicator functions (return pandas Series)
# ---------------------------------------------------------------------------

def compute_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    arr = df[column].to_numpy(dtype=np.float64)
    result = _ema_kernel(arr, period)
    return pd.Series(result, index=df.index, name=f"EMA_{period}")


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    arr = df["close"].to_numpy(dtype=np.float64)
    result = _rsi_kernel(arr, period)
    return pd.Series(result, index=df.index, name=f"RSI_{period}")


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"].to_numpy(dtype=np.float64)
    low   = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    result = _atr_kernel(high, low, close, period)
    return pd.Series(result, index=df.index, name=f"ATR_{period}")


def compute_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Returns DataFrame with columns: macd_line, signal_line, histogram."""
    arr = df["close"].to_numpy(dtype=np.float64)
    fast_ema = _ema_kernel(arr, fast)
    slow_ema = _ema_kernel(arr, slow)
    macd_line = fast_ema - slow_ema
    signal_line = _ema_kernel(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd_line":   macd_line,
            "signal_line": signal_line,
            "histogram":   histogram,
        },
        index=df.index,
    )


def compute_bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Returns DataFrame with columns: bb_mid, bb_upper, bb_lower, bb_width."""
    arr = df["close"].to_numpy(dtype=np.float64)
    mid  = _sma_kernel(arr, period)
    std  = _stddev_kernel(arr, period)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width},
        index=df.index,
    )


def compute_stochastic(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3
) -> pd.DataFrame:
    high  = df["high"].to_numpy(dtype=np.float64)
    low   = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    k_arr, d_arr = _stochastic_kernel(high, low, close, k_period, d_period)
    return pd.DataFrame(
        {"stoch_k": k_arr, "stoch_d": d_arr},
        index=df.index,
    )


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Session VWAP (resets daily).
    Requires 'volume' column and DatetimeIndex with timezone info.
    """
    if "volume" not in df.columns or df["volume"].sum() == 0:
        return pd.Series(np.nan, index=df.index, name="VWAP")

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = typical_price * df["volume"]

    date_key = df.index.date
    cum_tp_vol = tp_vol.groupby(date_key).cumsum()
    cum_vol    = df["volume"].groupby(date_key).cumsum()
    vwap = cum_tp_vol / cum_vol
    vwap.name = "VWAP"
    return vwap


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average Directional Index — measures trend strength (not direction).
    Returns DataFrame with: +DI, -DI, ADX.
    """
    high  = df["high"].to_numpy(dtype=np.float64)
    low   = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    n = len(close)

    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up   = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i]  = up   if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0

    atr = _atr_kernel(high, low, close, period)
    sm_plus  = _ema_kernel(plus_dm,  period)
    sm_minus = _ema_kernel(minus_dm, period)

    plus_di  = 100.0 * sm_plus  / np.where(atr == 0, 1e-9, atr)
    minus_di = 100.0 * sm_minus / np.where(atr == 0, 1e-9, atr)

    dx = 100.0 * np.abs(plus_di - minus_di) / np.where(
        (plus_di + minus_di) == 0, 1e-9, plus_di + minus_di
    )
    adx = _ema_kernel(dx, period)

    return pd.DataFrame(
        {"+DI": plus_di, "-DI": minus_di, "ADX": adx},
        index=df.index,
    )


# ---------------------------------------------------------------------------
# Indicator Service — registry / cache layer
# ---------------------------------------------------------------------------

_CacheKey = Tuple[str, str, str]  # (symbol, timeframe, indicator_name)


class IndicatorService:
    """
    Caches computed indicator Series so Strategy Engine can query the
    latest value of any indicator without re-running the full computation
    on every tick.

    Usage
    -----
    The service is called once per BarEvent with the latest bar DataFrame.
    Strategies then call `get(symbol, timeframe, "RSI_14")` to read the
    last value.

    Cache eviction
    --------------
    Uses a simple LRU dict capped at `max_cache_size` keys. In production
    this would be backed by Redis with a TTL equal to the bar period.
    """

    def __init__(self, max_cache_size: int = 500) -> None:
        self._cache: Dict[_CacheKey, pd.Series] = {}
        self._max_cache = max_cache_size

    def compute_all(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        config: Optional[dict] = None,
    ) -> Dict[str, pd.Series]:
        """
        Compute a standard set of indicators for a bar DataFrame and cache
        the results.  Returns a dict of indicator_name → Series.
        """
        if df is None or len(df) < 30:
            return {}

        cfg = config or {}
        results: Dict[str, pd.Series] = {}

        try:
            # EMAs
            for p in cfg.get("ema_periods", [9, 21, 50, 200]):
                name = f"EMA_{p}"
                s = compute_ema(df, p)
                results[name] = s
                self._cache[(symbol, timeframe, name)] = s

            # RSI
            rsi_period = cfg.get("rsi_period", 14)
            s = compute_rsi(df, rsi_period)
            results[f"RSI_{rsi_period}"] = s
            self._cache[(symbol, timeframe, f"RSI_{rsi_period}")] = s

            # ATR
            atr_period = cfg.get("atr_period", 14)
            s = compute_atr(df, atr_period)
            results[f"ATR_{atr_period}"] = s
            self._cache[(symbol, timeframe, f"ATR_{atr_period}")] = s

            # MACD
            macd_df = compute_macd(df)
            for col in macd_df.columns:
                results[col] = macd_df[col]
                self._cache[(symbol, timeframe, col)] = macd_df[col]

            # Bollinger Bands
            bb_df = compute_bollinger_bands(df)
            for col in bb_df.columns:
                results[col] = bb_df[col]
                self._cache[(symbol, timeframe, col)] = bb_df[col]

            # ADX
            adx_df = compute_adx(df)
            for col in adx_df.columns:
                results[col] = adx_df[col]
                self._cache[(symbol, timeframe, col)] = adx_df[col]

            # VWAP (only if volume data available)
            if "volume" in df.columns:
                s = compute_vwap(df)
                results["VWAP"] = s
                self._cache[(symbol, timeframe, "VWAP")] = s

            # Evict oldest entries if cache is full
            if len(self._cache) > self._max_cache:
                keys_to_remove = list(self._cache.keys())[:100]
                for k in keys_to_remove:
                    del self._cache[k]

        except Exception as exc:
            logger.error("Indicator computation failed for %s: %s", symbol, exc, exc_info=True)

        return results

    def get(self, symbol: str, timeframe: str, indicator_name: str) -> Optional[pd.Series]:
        """Return the cached Series for an indicator, or None if not computed."""
        return self._cache.get((symbol, timeframe, indicator_name))

    def get_last_value(self, symbol: str, timeframe: str, indicator_name: str) -> Optional[float]:
        """Return the last non-NaN value of an indicator."""
        series = self.get(symbol, timeframe, indicator_name)
        if series is None or series.empty:
            return None
        non_nan = series.dropna()
        return float(non_nan.iloc[-1]) if not non_nan.empty else None

    def invalidate(self, symbol: str, timeframe: str) -> None:
        """Clear all cached indicators for a symbol/timeframe pair."""
        to_remove = [k for k in self._cache if k[0] == symbol and k[1] == timeframe]
        for k in to_remove:
            del self._cache[k]
        logger.debug("Indicator cache invalidated: %s %s", symbol, timeframe)
