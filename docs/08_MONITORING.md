# 08 — Monitoring & Observabilité

> **Niveau** : DevOps Engineers, SRE, Senior Engineers  
> **Fichiers** : `monitoring/metrics.py`, `docker/prometheus.yml`, `docker/alert_rules.yml`

---

## Table des matières

1. [Piliers de l'observabilité](#1-piliers-de-lobservabilité)
2. [Métriques Prometheus](#2-métriques-prometheus)
3. [Logging structuré (JSON)](#3-logging-structuré-json)
4. [Health Checks](#4-health-checks)
5. [Stack Grafana / Loki / Jaeger](#5-stack-grafana--loki--jaeger)
6. [Alertes Prometheus](#6-alertes-prometheus)
7. [Dashboards recommandés](#7-dashboards-recommandés)
8. [Notification Service](#8-notification-service)
9. [Runbook des alertes critiques](#9-runbook-des-alertes-critiques)
10. [Considérations production](#10-considérations-production)

---

## 1. Piliers de l'observabilité

La plateforme implémente les 3 piliers d'observabilité (**observability pillars**) :

```
┌──────────────┬──────────────────────┬───────────────────────────────────┐
│    Pilier     │     Implémentation   │     Usage                         │
├──────────────┼──────────────────────┼───────────────────────────────────┤
│  MÉTRIQUES   │  Prometheus + Grafana│  Monitoring temps réel, alertes   │
│              │  25 métriques définies│  SLA, performance, santé          │
├──────────────┼──────────────────────┼───────────────────────────────────┤
│  LOGS        │  Structured JSON     │  Debugging, audit trail            │
│              │  Loki + Promtail     │  Recherche par correlation_id      │
├──────────────┼──────────────────────┼───────────────────────────────────┤
│  TRACES      │  Jaeger / OTLP       │  Latence end-to-end               │
│              │  OpenTelemetry       │  Goulots d'étranglement            │
└──────────────┴──────────────────────┴───────────────────────────────────┘
```

---

## 2. Métriques Prometheus

### Singleton `TradingMetrics`

Toutes les métriques sont enregistrées **une seule fois** dans un singleton. Prometheus lève une exception si on enregistre deux fois le même nom.

```python
class TradingMetrics:
    _instance: Optional["TradingMetrics"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_metrics()
        return cls._instance

# Accès global
metrics = TradingMetrics()
metrics.tick_received_total.labels(symbol="EURUSD", source="mt5").inc()
```

### Inventaire complet des 25 métriques

#### Market Data

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_market_data_ticks_total` | Counter | `symbol`, `source` | Ticks reçus depuis le démarrage |
| `trading_market_data_bars_total` | Counter | `symbol`, `timeframe` | Barres reçues |
| `trading_market_data_latency_seconds` | Histogram | `symbol` | Latence de traitement d'un tick |

#### Strategy Engine

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_strategy_signals_total` | Counter | `strategy_id`, `symbol`, `direction` | Signaux générés |
| `trading_strategy_signal_latency_seconds` | Histogram | `strategy_id` | Latence on_bar() → signal |

#### Risk Engine

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_risk_validations_total` | Counter | `result` (approved/rejected) | Validations |
| `trading_risk_breaches_total` | Counter | `breach_type` | Breaches par type |
| `trading_risk_drawdown_pct` | Gauge | — | Drawdown courant (%) |
| `trading_risk_var_99_usd` | Gauge | — | VaR 99% en USD |

#### Execution Engine

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_execution_orders_total` | Counter | `symbol`, `side`, `algorithm` | Ordres soumis |
| `trading_execution_fills_total` | Counter | `symbol`, `side` | Fills reçus |
| `trading_execution_round_trip_seconds` | Histogram | `symbol`, `algorithm` | Latence signal → fill |
| `trading_execution_slippage_bps` | Histogram | `symbol` | Slippage en bps |

#### Portfolio

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_portfolio_equity_usd` | Gauge | — | Equity totale en USD |
| `trading_portfolio_open_positions` | Gauge | — | Positions ouvertes |
| `trading_portfolio_realised_pnl_usd` | Gauge | — | P&L réalisé cumulatif |
| `trading_portfolio_unrealised_pnl_usd` | Gauge | — | P&L non-réalisé courant |

#### ML Engine

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_ml_prediction_confidence` | Histogram | `symbol`, `model` | Distribution des probabilités |
| `trading_ml_regime` | Gauge | `symbol` | Régime courant (0-3) |

#### Event Bus

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_eventbus_published_total` | Counter | `channel` | Événements publiés |
| `trading_eventbus_consumed_total` | Counter | `channel` | Événements consommés |
| `trading_eventbus_processing_seconds` | Histogram | `channel` | Temps de traitement handler |

#### API & Système

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `trading_api_requests_total` | Counter | `method`, `endpoint`, `status_code` | Requêtes HTTP |
| `trading_api_request_latency_seconds` | Histogram | `method`, `endpoint` | Latence HTTP |
| `trading_api_ws_connections_active` | Gauge | — | Connexions WebSocket actives |
| `trading_circuit_breaker_open` | Gauge | — | 1 si circuit breaker ouvert |
| `trading_platform_uptime_seconds` | Gauge | — | Uptime depuis démarrage |

### Buckets de latence personnalisés

```python
# Buckets couvrant sub-milliseconde → dizaines de secondes
# Adaptés aux différentes latences du trading
LATENCY_BUCKETS = [
    0.0001,   # 0.1ms — tick processing target
    0.001,    # 1ms   — indicator calculation
    0.005,    # 5ms   — signal generation
    0.01,     # 10ms  — risk validation
    0.05,     # 50ms  — order submission
    0.1,      # 100ms — MT5 round-trip
    0.5,      # 500ms — TWAP slice
    1.0,      # 1s    — slow path
    5.0,      # 5s    — backtest operation
    10.0,     # 10s   — warm-up
]
```

### Utilisation dans le code

```python
# Dans le hot-path (market data)
with metrics.data_latency_seconds.labels(symbol=tick.symbol).time():
    await indicator_service.compute_all(symbol, tf, df)

# Counter simple
metrics.tick_received_total.labels(
    symbol=tick.symbol,
    source=tick.source,
).inc()

# Gauge (P&L, drawdown)
metrics.equity_usd.set(float(portfolio.get_equity()))
metrics.current_drawdown_pct.set(portfolio.get_current_drawdown() * 100)

# Histogram observation
metrics.slippage_bps.labels(symbol=fill.symbol).observe(
    abs(fill.slippage / fill.fill_price * 10000)
)
```

### Endpoint `/metrics`

```bash
curl http://localhost:8000/metrics

# trading_portfolio_equity_usd 102350.45
# trading_risk_drawdown_pct 1.23
# trading_execution_round_trip_seconds_bucket{symbol="EURUSD",algorithm="MARKET",le="0.1"} 487
# trading_strategy_signals_total{strategy_id="ema_crossover_v1",direction="LONG"} 23
```

---

## 3. Logging structuré (JSON)

### Format JSON (production)

```json
{
  "timestamp": "2024-02-01T10:30:15.432Z",
  "level":     "INFO",
  "logger":    "execution_engine.service",
  "message":   "Fill received: BUY EURUSD 10000 @ 1.08514",
  "module":    "service",
  "line":      287,
  "symbol":    "EURUSD",
  "side":      "BUY",
  "quantity":  10000,
  "fill_price": 1.08514,
  "commission": 7.0,
  "order_id":  "3f7e2c1a-...",
  "correlation_id": "a1b2c3d4-..."
}
```

### Configuration

```python
# monitoring/metrics.py
def configure_logging(level: str = "INFO", json_format: bool = True):
    root    = logging.getLogger()
    handler = logging.StreamHandler()

    if json_format:
        handler.setFormatter(JSONFormatter())  # Pour production
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
        )  # Pour développement

    root.addHandler(handler)
    root.setLevel(getattr(logging, level))
```

### Niveaux de log

| Niveau | Usage |
|---|---|
| `DEBUG` | Détails de calcul (indicateurs, features ML) |
| `INFO` | Événements normaux (fills, signaux, start/stop) |
| `WARNING` | Conditions dégradées (reconnexion broker, cache miss) |
| `ERROR` | Erreurs récupérables (fill échoué, requête DB timeout) |
| `CRITICAL` | Circuit breaker, pertes dépassant les limites |

### Corrélation des logs

Chaque événement de domaine porte un `correlation_id` (UUID v4). Cet ID est propagé à travers toute la chaîne :

```
BarEvent(correlation_id="abc123")
    → SignalEvent(correlation_id="abc123")
    → OrderEvent(correlation_id="abc123")
    → FillEvent(correlation_id="abc123")

# Requête Loki pour tracer toute la chaîne
{app="trading"} | json | correlation_id="abc123"
```

---

## 4. Health Checks

### Architecture hiérarchique

```
GET /health                          → Réponse < 10ms (K8s liveness probe)
  {"status": "ok", "uptime": 3600}

GET /health/detailed                 → Réponse < 500ms (readiness probe)
  {
    "redis":     {"healthy": true,  "latency_ms": 0.8},
    "postgres":  {"healthy": true,  "latency_ms": 2.1},
    "mt5_broker":{"healthy": true,  "latency_ms": 18.5},
    "event_bus": {"healthy": true,  "latency_ms": 1.2},
    "strategy":  {"healthy": true,  "latency_ms": 0.1}
  }
```

### Enregistrement des checks

```python
health_checker = HealthChecker()

# Redis
async def check_redis():
    t0 = time.perf_counter()
    await redis_client.ping()
    return True, f"Redis OK ({(time.perf_counter()-t0)*1000:.1f}ms)"

health_checker.register("redis", check_redis)

# Broker
async def check_broker():
    connected = broker._connected
    return connected, "MT5 connected" if connected else "MT5 disconnected"

health_checker.register("mt5_broker", check_broker)
```

### Intégration Docker

```yaml
# docker-compose.yml
trading-core:
  healthcheck:
    test:     ["CMD", "curl", "-f", "http://localhost:8001/health"]
    interval: 30s
    timeout:  10s
    retries:  3
    start_period: 30s  # temps de warm-up (Numba JIT)
```

---

## 5. Stack Grafana / Loki / Jaeger

### Architecture complète

```
                     ┌─────────────────┐
                     │     Grafana      │
                     │   (port 3000)    │
                     └────────┬────────┘
                              │ query
                ┌─────────────┼─────────────┐
                │             │             │
                ▼             ▼             ▼
          Prometheus        Loki         Jaeger
          (métriques)      (logs)       (traces)
               ▲             ▲             ▲
               │             │             │
         trading-core    Promtail      OpenTelemetry
          /metrics      (log scraper)   SDK
```

### Datasources Grafana

```yaml
# docker/grafana/datasources/datasources.yml
apiVersion: 1
datasources:
  - name:    Prometheus
    type:    prometheus
    url:     http://prometheus:9090
    default: true

  - name: Loki
    type: loki
    url:  http://loki:3100

  - name: Jaeger
    type: jaeger
    url:  http://jaeger:16686
```

### Loki — indexation des logs

```yaml
# docker/loki-config.yaml
schema_config:
  configs:
    - from: 2024-01-01
      store: boltdb-shipper
      object_store: filesystem
      schema: v11
      index:
        prefix: index_
        period: 24h
```

---

## 6. Alertes Prometheus

### Règles définies (`docker/alert_rules.yml`)

#### Alertes Portfolio / Risk

```yaml
- alert: DrawdownExceeds10Pct
  expr:  trading_risk_drawdown_pct > 10
  for:   0m    # Immédiat
  labels:
    severity: warning
  annotations:
    summary: "Drawdown > 10%: {{ $value | printf \"%.2f\" }}%"

- alert: DrawdownExceeds15Pct
  expr:  trading_risk_drawdown_pct > 15
  for:   0m    # Immédiat — critique
  labels:
    severity: critical
  annotations:
    summary: "CRITICAL: Drawdown > 15% — circuit breaker may have fired"

- alert: CircuitBreakerOpen
  expr:  trading_circuit_breaker_open == 1
  for:   0m
  labels:
    severity: critical
  annotations:
    summary: "Trading HALTED — circuit breaker is OPEN"
```

#### Alertes Exécution

```yaml
- alert: HighSlippage
  expr: histogram_quantile(0.95,
          rate(trading_execution_slippage_bps_bucket[5m])
        ) > 5
  for:  5m
  annotations:
    summary: "P95 slippage > 5bps over 5 minutes"

- alert: OrderRoundTripSlow
  expr: histogram_quantile(0.99,
          rate(trading_execution_round_trip_seconds_bucket[5m])
        ) > 2
  for:  5m
  annotations:
    summary: "P99 round-trip > 2 seconds"
```

#### Alertes Système

```yaml
- alert: TradingCorePlatformDown
  expr:  up{job="trading-core"} == 0
  for:   1m
  labels:
    severity: critical
  annotations:
    summary: "trading-core service DOWN — all trading halted"

- alert: NoSignalsGenerated
  expr:  rate(trading_strategy_signals_total[30m]) == 0
  for:   30m
  annotations:
    summary: "No signals in 30 minutes — check strategy engine"
```

### Alertmanager routing

```yaml
# docker/alertmanager.yml
route:
  group_by:    ['alertname', 'severity']
  group_wait:  30s
  receiver:    telegram-critical

  routes:
    - match:
        severity: critical
      receiver:   pagerduty-critical

    - match:
        severity: warning
      receiver:   slack-warnings

receivers:
  - name: telegram-critical
    webhook_configs:
      - url: 'http://fastapi:8000/internal/alertmanager'

  - name: pagerduty-critical
    pagerduty_configs:
      - service_key: $PAGERDUTY_KEY
        severity:    critical
```

---

## 7. Dashboards recommandés

### Dashboard 1 — Trading Overview (temps réel)

Panels recommandés :

```
Row 1 — P&L (actualisation 1s)
  [Equity gauge]  [Drawdown gauge]  [PnL total]  [Positions ouvertes]

Row 2 — Signaux et exécution
  [Signals/min graph]  [Fill latency histogram]  [Slippage distribution]

Row 3 — Risk
  [VaR 99% gauge]  [Circuit breaker status]  [Breach history]
```

### Dashboard 2 — System Health

```
Row 1 — Services
  [Redis ping latency]  [DB connection pool]  [MT5 connection]

Row 2 — Event Bus
  [Events/sec par channel]  [Backlog (published - consumed)]  [Consumer lag]

Row 3 — API
  [Requests/sec]  [Error rate]  [P99 latency]
```

### Panel Grafana : Equity Curve

```
# PromQL
trading_portfolio_equity_usd

# Paramètres
Panel type: Time series
Fill opacity: 10
Line width: 2
Color: Green si > initial_capital, Rouge sinon
```

### Panel Grafana : Drawdown en temps réel

```
# PromQL
trading_risk_drawdown_pct

# Thresholds
< 5%  : vert
5-10% : jaune  
10-15%: orange
> 15% : rouge (alerte critique)
```

---

## 8. Notification Service

Le service de notification est entièrement séparé de la logique de trading. Il s'abonne aux events Redis et route les alertes vers les canaux appropriés.

### Matrice de routing

| Niveau | Telegram | Email | Webhook (Slack) |
|---|---|---|---|
| DEBUG | ❌ | ❌ | ❌ |
| INFO | ❌ | ❌ | ✅ |
| WARNING | ✅ | ❌ | ✅ |
| ERROR | ✅ | ✅ | ✅ |
| CRITICAL | ✅ | ✅ | ✅ |

### Rate limiting Telegram

```python
# Token bucket : 25 messages/sec, burst de 30
class TokenBucket:
    def __init__(self, rate=25, capacity=30):
        self._rate     = rate
        self._capacity = capacity
        self._tokens   = capacity
        self._last     = time.monotonic()

    async def acquire(self):
        while True:
            now            = time.monotonic()
            self._tokens   = min(
                self._capacity,
                self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep(0.1)
```

### Circuit Breaker par canal

```python
# Si un canal échoue 3× consécutives → backoff 60s
@dataclass
class ChannelCircuitBreaker:
    failures:      int   = 0
    threshold:     int   = 3
    backoff_until: float = 0.0

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.backoff_until = time.monotonic() + 60.0

    def is_open(self) -> bool:
        if time.monotonic() > self.backoff_until:
            self.failures = 0
            return False
        return self.failures >= self.threshold
```

---

## 9. Runbook des alertes critiques

### ALERT: `TradingCorePlatformDown`

```
Symptôme : La plateforme ne répond plus
Actions  :
  1. docker logs trading-core --tail 100
  2. Vérifier Redis: redis-cli ping
  3. Vérifier DB: psql -h localhost -U trading -c "SELECT 1"
  4. Redémarrer: docker restart trading-core
  5. Si persiste: docker compose down && docker compose up -d
```

### ALERT: `CircuitBreakerOpen`

```
Symptôme : Trading halté, circuit breaker ouvert
Actions  :
  1. Vérifier les positions ouvertes (ne pas réouvrir avant analyse)
  2. Lire les logs pour la cause: grep "CIRCUIT BREAKER" /var/log/trading.log
  3. Si drawdown excessif: analyser les 10 derniers trades
  4. Si cause résolue: POST /api/v1/risk/circuit-breaker/reset (admin JWT)
  5. Redémarrer avec taille réduite (50% du sizing normal)
```

### ALERT: `DrawdownExceeds15Pct`

```
Symptôme : Drawdown > 15%
Actions  :
  1. Vérifier si circuit breaker déjà ouvert
  2. Analyser la stratégie défaillante (dashboard Positions)
  3. Désactiver la stratégie: POST /api/v1/strategies/{id}/deactivate
  4. Analyser les conditions de marché (régime ?)
  5. Ne pas relancer sans revue manuelle
```

---

## 10. Considérations production

### SLOs recommandés

| SLO | Cible |
|---|---|
| Disponibilité de la plateforme | 99.9% (hors maintenance broker) |
| Latence P99 tick → signal | < 10ms |
| Latence P99 signal → fill confirmation | < 200ms |
| Taux d'erreur API | < 0.1% |
| Perte max sur alerte non reçue | < 1 minute |

### Retention des données

| Données | Retention |
|---|---|
| Métriques Prometheus | 90 jours |
| Logs Loki | 30 jours |
| Traces Jaeger | 7 jours |
| OHLCV TimescaleDB | Indéfini (compressés après 7j) |
| Audit log | Indéfini (append-only) |

---

*Document précédent → [07_MACHINE_LEARNING.md](07_MACHINE_LEARNING.md)*  
*Document suivant → [09_DEPLOYMENT.md](09_DEPLOYMENT.md)*
