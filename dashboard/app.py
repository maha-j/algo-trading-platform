"""
Trading Platform Dashboard
==========================
Streamlit-based real-time monitoring dashboard.

Pages
-----
1. Overview     — equity curve, open P&L, key metrics at a glance.
2. Positions    — live positions with mark-to-market P&L.
3. Strategies   — strategy status, activate/deactivate controls.
4. Risk Monitor — drawdown gauge, VaR, circuit breaker status.
5. Backtest     — run and visualise backtests interactively.
6. System       — health checks, event bus throughput, latency histograms.

Design decisions
----------------
* All data is fetched via the FastAPI REST backend, not by importing engines
  directly.  This keeps the dashboard stateless and horizontally scalable.
* Auto-refresh via st.rerun() at configurable intervals.
* Plotly is used for all charts (interactive, exports to PNG/SVG).
* st.cache_data with TTL prevents hammering the API on every rerender.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_TOKEN = os.getenv("API_JWT_TOKEN", "")      # Injected at runtime
REFRESH_INTERVAL_SEC = 5                         # Auto-refresh every N seconds

st.set_page_config(
    page_title="Trading Platform Monitor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS — dark trading terminal aesthetic
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .stMetric { background: #1c1e26; border-radius: 8px; padding: 16px; }
    .stMetric .metric-value { font-size: 28px; font-weight: 700; }
    .positive { color: #00d4aa; }
    .negative { color: #ff4444; }
    div[data-testid="stMetricDelta"] svg { display: none; }
    h1, h2, h3 { color: #e0e0e0; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------

def get_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    return headers


@st.cache_data(ttl=3)
def fetch_portfolio() -> Optional[dict]:
    try:
        r = httpx.get(f"{API_BASE}/api/v1/portfolio/summary", headers=get_headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Portfolio API unavailable: {e}")
        return None


@st.cache_data(ttl=3)
def fetch_equity_curve(limit: int = 500) -> pd.DataFrame:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/portfolio/equity-curve",
            params={"limit": limit},
            headers=get_headers(),
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=10)
def fetch_risk_metrics() -> Optional[dict]:
    try:
        r = httpx.get(f"{API_BASE}/api/v1/risk/metrics", headers=get_headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


@st.cache_data(ttl=5)
def fetch_strategies() -> List[dict]:
    try:
        r = httpx.get(f"{API_BASE}/api/v1/strategies/", headers=get_headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_bars(symbol: str, timeframe: str, lookback: int) -> pd.DataFrame:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/market-data/bars/{symbol}",
            params={"timeframe": timeframe, "lookback": lookback},
            headers=get_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        df = pd.DataFrame(data)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        return df
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------

def equity_curve_chart(df: pd.DataFrame, initial_capital: float) -> go.Figure:
    fig = go.Figure()
    color = "#00d4aa" if df["equity"].iloc[-1] >= initial_capital else "#ff4444"
    fig.add_trace(go.Scatter(
        x=df["timestamp"],
        y=df["equity"],
        mode="lines",
        name="Equity",
        line=dict(color=color, width=2),
        fill="tozeroy",
        fillcolor=f"rgba({'0,212,170' if color=='#00d4aa' else '255,68,68'},0.1)",
    ))
    fig.add_hline(
        y=initial_capital, line_dash="dash",
        line_color="rgba(255,255,255,0.3)", annotation_text="Initial Capital"
    )
    fig.update_layout(
        template="plotly_dark",
        title="Equity Curve",
        xaxis_title="Time",
        yaxis_title="Equity (USD)",
        height=350,
        margin=dict(l=0, r=0, t=40, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(28,30,38,1)",
    )
    return fig


def candlestick_chart(df: pd.DataFrame, symbol: str) -> go.Figure:
    fig = go.Figure(data=[go.Candlestick(
        x=df.index,
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        increasing_line_color="#00d4aa",
        decreasing_line_color="#ff4444",
        name=symbol,
    )])
    fig.update_layout(
        template="plotly_dark",
        title=f"{symbol} Price",
        xaxis_rangeslider_visible=False,
        height=400,
        margin=dict(l=0, r=0, t=40, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(28,30,38,1)",
    )
    return fig


def drawdown_gauge(drawdown_pct: float) -> go.Figure:
    color = "#00d4aa" if drawdown_pct < 5 else "#ffcc00" if drawdown_pct < 10 else "#ff4444"
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=abs(drawdown_pct),
        title={"text": "Drawdown %", "font": {"color": "#e0e0e0"}},
        gauge={
            "axis": {"range": [0, 20], "tickcolor": "#888"},
            "bar": {"color": color},
            "bgcolor": "#1c1e26",
            "bordercolor": "#333",
            "steps": [
                {"range": [0, 5],  "color": "rgba(0,212,170,0.1)"},
                {"range": [5, 10], "color": "rgba(255,204,0,0.1)"},
                {"range": [10, 20],"color": "rgba(255,68,68,0.1)"},
            ],
            "threshold": {"line": {"color": "#ff4444", "width": 3}, "value": 15},
        },
        number={"suffix": "%", "font": {"size": 36, "color": color}},
    ))
    fig.update_layout(
        template="plotly_dark",
        height=250,
        margin=dict(l=20, r=20, t=40, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚡ Trading Platform")
    st.markdown("---")
    page = st.radio(
        "Navigation",
        ["📊 Overview", "📋 Positions", "🤖 Strategies", "🛡️ Risk", "🔬 Backtest", "⚙️ System"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    auto_refresh = st.toggle("Auto-refresh", value=True)
    refresh_sec  = st.slider("Refresh interval (s)", 2, 30, REFRESH_INTERVAL_SEC)
    if auto_refresh:
        time.sleep(refresh_sec)
        st.rerun()
    st.markdown("---")
    st.caption(f"Last update: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")


# ===========================================================================
# Page: Overview
# ===========================================================================

if page == "📊 Overview":
    st.title("Platform Overview")

    portfolio = fetch_portfolio()
    if not portfolio:
        st.warning("Unable to connect to trading platform API. Check that the service is running.")
        st.stop()

    # KPI row
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    equity    = portfolio["equity"]
    initial   = portfolio["initial_capital"]
    total_pnl = portfolio["total_pnl"]
    dd_pct    = portfolio["drawdown_pct"]
    pnl_color = "normal" if total_pnl >= 0 else "inverse"

    col1.metric("Equity", f"${equity:,.2f}",
                delta=f"${total_pnl:+,.2f}", delta_color=pnl_color)
    col2.metric("Realised P&L", f"${portfolio['realised_pnl']:+,.2f}", delta_color=pnl_color)
    col3.metric("Unrealised P&L", f"${portfolio['unrealised_pnl']:+,.2f}", delta_color=pnl_color)
    col4.metric("Drawdown", f"{dd_pct:.2f}%",
                delta=f"{'▲' if dd_pct < 5 else '▼'} {'Low' if dd_pct < 5 else 'High'}",
                delta_color="normal" if dd_pct < 5 else "inverse")
    col5.metric("Open Positions", portfolio["open_positions"])
    col6.metric("Exposure", f"{portfolio['exposure_pct']:.1f}%")

    st.markdown("---")

    # Equity curve
    eq_df = fetch_equity_curve(500)
    if not eq_df.empty:
        st.plotly_chart(
            equity_curve_chart(eq_df, initial),
            use_container_width=True,
        )
    else:
        st.info("No equity curve data yet. Start trading to see the chart.")

    # Positions table
    if portfolio["positions"]:
        st.subheader("Open Positions")
        pos_df = pd.DataFrame(portfolio["positions"])
        pos_df["pnl_color"] = pos_df["unrealised_pnl"].apply(
            lambda x: "🟢" if x >= 0 else "🔴"
        )
        st.dataframe(
            pos_df[["symbol", "asset_class", "net_qty", "avg_entry",
                    "current_price", "unrealised_pnl", "market_value"]].rename(columns={
                "symbol": "Symbol", "asset_class": "Class",
                "net_qty": "Net Qty", "avg_entry": "Avg Entry",
                "current_price": "Price", "unrealised_pnl": "Unreal. P&L",
                "market_value": "Market Value",
            }),
            use_container_width=True,
        )


# ===========================================================================
# Page: Positions
# ===========================================================================

elif page == "📋 Positions":
    st.title("Open Positions")

    portfolio = fetch_portfolio()
    if not portfolio:
        st.warning("API unavailable")
        st.stop()

    if not portfolio["positions"]:
        st.info("No open positions.")
    else:
        for pos in portfolio["positions"]:
            col1, col2, col3, col4 = st.columns(4)
            pnl = pos["unrealised_pnl"]
            pnl_str = f"${pnl:+.2f}"
            color_class = "positive" if pnl >= 0 else "negative"
            col1.metric(pos["symbol"], f"{pos['net_qty']} {'📈' if pos['net_qty'] > 0 else '📉'}")
            col2.metric("Avg Entry", f"{pos['avg_entry']:.5f}")
            col3.metric("Current", f"{pos['current_price']:.5f}")
            col4.metric("Unreal. P&L", pnl_str, delta_color="normal" if pnl >= 0 else "inverse")
            st.markdown("---")


# ===========================================================================
# Page: Strategies
# ===========================================================================

elif page == "🤖 Strategies":
    st.title("Strategy Engine")

    strategies = fetch_strategies()
    if not strategies:
        st.info("No strategies registered.")
    else:
        for s in strategies:
            col1, col2, col3 = st.columns([3, 1, 1])
            status_icon = "🟢" if s["is_active"] else "🔴"
            col1.markdown(f"**{status_icon} {s['strategy_id']}**  \n"
                          f"Symbols: `{', '.join(s['symbols'])}` | "
                          f"Indicators: {s['indicators_attached']}")
            if s["is_active"]:
                if col2.button("Deactivate", key=f"deact_{s['strategy_id']}"):
                    try:
                        r = httpx.post(
                            f"{API_BASE}/api/v1/strategies/{s['strategy_id']}/deactivate",
                            headers=get_headers(), timeout=5,
                        )
                        st.success(f"Strategy {s['strategy_id']} deactivated") if r.status_code == 200 else st.error(r.text)
                    except Exception as e:
                        st.error(str(e))
            else:
                if col3.button("Activate", key=f"act_{s['strategy_id']}"):
                    try:
                        r = httpx.post(
                            f"{API_BASE}/api/v1/strategies/{s['strategy_id']}/activate",
                            headers=get_headers(), timeout=5,
                        )
                        st.success(f"Strategy {s['strategy_id']} activated") if r.status_code == 200 else st.error(r.text)
                    except Exception as e:
                        st.error(str(e))
            st.markdown("---")


# ===========================================================================
# Page: Risk Monitor
# ===========================================================================

elif page == "🛡️ Risk":
    st.title("Risk Monitor")

    risk = fetch_risk_metrics()
    if not risk:
        st.warning("Risk metrics unavailable")
        st.stop()

    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(drawdown_gauge(risk["current_drawdown_pct"]), use_container_width=True)
    with col2:
        st.markdown("### Risk Limits")
        st.metric("VaR 99%", f"{risk['var_99_pct']:.3f}%")
        st.metric("CVaR 99%", f"{risk['cvar_99_pct']:.3f}%")
        st.metric("Max DD Limit", f"{risk['max_drawdown_limit_pct']:.1f}%")
        st.metric("Max Position Size", f"{risk['max_position_size_pct']:.1f}%")

    if risk["circuit_breaker_open"]:
        st.error("🚨 CIRCUIT BREAKER OPEN — All trading is halted. Contact risk management.")
        if st.button("Reset Circuit Breaker (Admin only)"):
            st.warning("Circuit breaker reset requires admin credentials. Use API directly.")
    else:
        st.success("✅ Circuit breaker closed — Trading active")


# ===========================================================================
# Page: Backtest
# ===========================================================================

elif page == "🔬 Backtest":
    st.title("Interactive Backtest")

    with st.form("backtest_form"):
        col1, col2, col3 = st.columns(3)
        symbol     = col1.selectbox("Symbol", ["EURUSD", "GBPUSD", "BTCUSDT", "ETHUSDT", "SPY"])
        timeframe  = col2.selectbox("Timeframe", ["M15", "H1", "H4", "D1"])
        lookback   = col3.number_input("Lookback bars", min_value=100, max_value=5000, value=1000)

        col4, col5 = st.columns(2)
        capital    = col4.number_input("Initial Capital ($)", min_value=1000.0, value=100000.0, step=1000.0)
        strategy   = col5.selectbox("Strategy", ["ema_crossover_v1"])

        walk_fwd   = st.checkbox("Run Walk-Forward Analysis", value=False)
        submitted  = st.form_submit_button("▶ Run Backtest", use_container_width=True)

    if submitted:
        with st.spinner(f"Running backtest on {lookback} bars of {symbol} {timeframe}..."):
            payload = {
                "symbol": symbol, "timeframe": timeframe, "strategy_id": strategy,
                "lookback_bars": int(lookback), "initial_capital": float(capital),
                "walk_forward": walk_fwd,
            }
            try:
                endpoint = "walk-forward" if walk_fwd else "run"
                r = httpx.post(
                    f"{API_BASE}/api/v1/backtest/{endpoint}",
                    json=payload, headers=get_headers(), timeout=120,
                )
                r.raise_for_status()
                result = r.json()

                if walk_fwd:
                    st.subheader("Walk-Forward Results")
                    summary_cols = st.columns(4)
                    summary_cols[0].metric("Avg OOS Sharpe",  f"{result.get('avg_oos_sharpe', 0):.3f}")
                    summary_cols[1].metric("Avg OOS Return",  f"{result.get('avg_oos_return', 0):.2f}%")
                    summary_cols[2].metric("Avg OOS Max DD",  f"{result.get('avg_oos_max_dd', 0):.2f}%")
                    summary_cols[3].metric("Consistency",     f"{result.get('consistency_pct', 0):.1f}%")
                    if "split_results" in result:
                        st.dataframe(pd.DataFrame(result["split_results"]), use_container_width=True)
                else:
                    stats = result
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total Return",    f"{stats['total_return_pct']:.2f}%")
                    col1.metric("Annual Return",   f"{stats['annualised_return_pct']:.2f}%")
                    col2.metric("Sharpe Ratio",    f"{stats['sharpe_ratio']:.3f}")
                    col2.metric("Sortino Ratio",   f"{stats['sortino_ratio']:.3f}")
                    col2.metric("Calmar Ratio",    f"{stats['calmar_ratio']:.3f}")
                    col3.metric("Max Drawdown",    f"{stats['max_drawdown_pct']:.2f}%")
                    col3.metric("Win Rate",        f"{stats['win_rate_pct']:.1f}%")
                    col3.metric("Profit Factor",   f"{stats['profit_factor']:.3f}")
                    st.info(f"Total trades: {stats['total_trades']} | Runtime: {stats['runtime_seconds']:.2f}s")
            except Exception as e:
                st.error(f"Backtest failed: {e}")


# ===========================================================================
# Page: System Health
# ===========================================================================

elif page == "⚙️ System":
    st.title("System Health")

    try:
        r = httpx.get(f"{API_BASE}/health/detailed", timeout=10)
        health = r.json()
        for name, status in health.items():
            if isinstance(status, dict):
                healthy = status.get("healthy", False)
                latency = status.get("latency_ms", 0)
                msg     = status.get("message", "")
                icon    = "🟢" if healthy else "🔴"
                st.markdown(f"{icon} **{name}** — {latency:.1f}ms {f'| {msg}' if msg else ''}")
    except Exception as e:
        st.error(f"Health check failed: {e}")

    st.markdown("---")
    st.subheader("Metrics Links")
    col1, col2, col3 = st.columns(3)
    col1.markdown("**[Grafana →](http://localhost:3000)**")
    col2.markdown("**[Prometheus →](http://localhost:9090)**")
    col3.markdown("**[Jaeger Traces →](http://localhost:16686)**")
