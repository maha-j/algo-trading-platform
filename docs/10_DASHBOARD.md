# 10 — Dashboard Streamlit

> **Niveau** : Engineers, Traders  
> **Fichiers** : `dashboard/app.py`

---

## Table des matières

1. [Architecture du dashboard](#1-architecture-du-dashboard)
2. [Pages et fonctionnalités](#2-pages-et-fonctionnalités)
3. [Connexion à l'API](#3-connexion-à-lapi)
4. [Charts Plotly](#4-charts-plotly)
5. [Auto-refresh et temps réel](#5-auto-refresh-et-temps-réel)
6. [Personnalisation et thème](#6-personnalisation-et-thème)

---

## 1. Architecture du dashboard

Le dashboard est **stateless** : il ne contient aucune logique de trading. Il lit uniquement via les endpoints REST de la FastAPI.

```
Dashboard (Streamlit)
       │
       │ httpx (REST calls)
       ▼
FastAPI Gateway (:8000)
       │
       ├── /api/v1/portfolio/summary
       ├── /api/v1/portfolio/equity-curve
       ├── /api/v1/risk/metrics
       ├── /api/v1/strategies/
       ├── /api/v1/market-data/bars/{symbol}
       └── /api/v1/backtest/run
```

**Avantage** : le dashboard peut tourner sur une machine différente de la plateforme, sans risque d'interférence avec le trading.

---

## 2. Pages et fonctionnalités

### Page 1 — Overview (temps réel)

```
╔══════════════════════════════════════════════════════════════╗
║  Equity     Realised P&L  Unrealised  Drawdown  Positions  Exposure
║  $102,350   +$2,350       -$215       1.23%     3           18.5%
╠══════════════════════════════════════════════════════════════╣
║                    Equity Curve                              ║
║  [Graphique linéaire temps réel avec fill area]             ║
╠══════════════════════════════════════════════════════════════╣
║  Open Positions Table                                        ║
║  EURUSD | LONG | 10,000 | 1.08512 | 1.08650 | +$138 | ...  ║
╚══════════════════════════════════════════════════════════════╝
```

### Page 2 — Positions

Vue détaillée de chaque position ouverte avec les métriques de P&L en temps réel.

### Page 3 — Strategies

- Liste des stratégies enregistrées
- Statut actif/inactif
- Boutons d'activation/désactivation (appellent POST /strategies/{id}/activate)

### Page 4 — Risk Monitor

- Jauge de drawdown (vert < 5%, jaune < 10%, rouge > 15%)
- VaR 99% et CVaR
- Status circuit breaker (alerte rouge si ouvert)

### Page 5 — Backtest

Interface interactive :
- Formulaire de paramètres (symbole, timeframe, stratégie, capital)
- Lancement du backtest via l'API
- Affichage des statistiques (Sharpe, Sortino, drawdown, win rate...)
- Option Walk-Forward

### Page 6 — System Health

- Status de tous les services (Redis, DB, MT5, event bus)
- Liens vers Grafana, Prometheus, Jaeger

---

## 3. Connexion à l'API

### Cache avec TTL

```python
@st.cache_data(ttl=3)    # actualisation toutes les 3 secondes
def fetch_portfolio() -> Optional[dict]:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/portfolio/summary",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Portfolio API unavailable: {e}")
        return None
```

Les TTL sont adaptés par type de donnée :
- `ttl=3` : portfolio, positions (temps réel)
- `ttl=10` : métriques risk
- `ttl=30` : données OHLCV pour charts
- `ttl=5` : stratégies

---

## 4. Charts Plotly

### Equity Curve interactive

```python
def equity_curve_chart(df, initial_capital) -> go.Figure:
    color = "#00d4aa" if df["equity"].iloc[-1] >= initial_capital else "#ff4444"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["equity"],
        mode="lines",
        fill="tozeroy",
        fillcolor=f"rgba({'0,212,170' if color=='#00d4aa' else '255,68,68'},0.1)",
        line=dict(color=color, width=2),
    ))
    fig.add_hline(y=initial_capital, line_dash="dash",
                  line_color="rgba(255,255,255,0.3)",
                  annotation_text="Initial Capital")
    fig.update_layout(template="plotly_dark", height=350)
    return fig
```

### Drawdown Gauge

```python
def drawdown_gauge(drawdown_pct) -> go.Figure:
    color = "#00d4aa" if drawdown_pct < 5 else "#ffcc00" if drawdown_pct < 10 else "#ff4444"
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=abs(drawdown_pct),
        gauge={
            "axis":  {"range": [0, 20]},
            "bar":   {"color": color},
            "steps": [
                {"range": [0, 5],   "color": "rgba(0,212,170,0.1)"},
                {"range": [5, 10],  "color": "rgba(255,204,0,0.1)"},
                {"range": [10, 20], "color": "rgba(255,68,68,0.1)"},
            ],
            "threshold": {"line": {"color": "#ff4444", "width": 3}, "value": 15},
        },
    ))
    return fig
```

---

## 5. Auto-refresh et temps réel

```python
# Sidebar — contrôle du refresh
auto_refresh  = st.toggle("Auto-refresh", value=True)
refresh_sec   = st.slider("Refresh interval (s)", 2, 30, 5)

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()   # Relance la page entière
```

### Limitations Streamlit vs WebSocket

Streamlit ne supporte pas nativement les WebSockets entrants. Pour un vrai temps réel :

```python
# Option 1: polling rapide (solution actuelle, simple)
time.sleep(2)
st.rerun()

# Option 2: st-autorefresh (package tiers)
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=2000, key="data_refresh")

# Option 3: composant custom WS (avancé)
# → lire les données WebSocket depuis l'API FastAPI
```

---

## 6. Personnalisation et thème

### Thème dark "trading terminal"

```python
st.markdown("""
<style>
    .main             { background-color: #0e1117; }
    .stMetric         { background: #1c1e26; border-radius: 8px; padding: 16px; }
    .positive         { color: #00d4aa; }   /* vert */
    .negative         { color: #ff4444; }   /* rouge */
</style>
""", unsafe_allow_html=True)
```

### Configuration `.streamlit/config.toml`

```toml
[theme]
base           = "dark"
primaryColor   = "#00d4aa"
backgroundColor = "#0e1117"
secondaryBackgroundColor = "#1c1e26"
textColor      = "#e0e0e0"

[server]
headless      = true
port          = 8501
enableCORS    = false
```

---

*Document précédent → [09_DEPLOYMENT.md](09_DEPLOYMENT.md)*  
*Document suivant → [11_SECURITY.md](11_SECURITY.md)*
