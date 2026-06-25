<div align="center">

# 🏦 Algo Trading Platform

**Plateforme de trading algorithmique institutionnelle**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Redis](https://img.shields.io/badge/Redis_Streams-7.2-DC382D?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![TimescaleDB](https://img.shields.io/badge/TimescaleDB-2.14-FDB515?style=flat-square&logo=postgresql&logoColor=white)](https://timescale.com)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-42%20passed-brightgreen?style=flat-square)](#testing)

*Multi-asset · Event-driven · Production-ready · Clean Architecture*

[📖 Documentation](#architecture) · [🚀 Quick Start](#quick-start) · [🧪 Tests](#testing) · [📊 Dashboard](#interfaces)

</div>

---

## 🎯 Vue d'ensemble

Plateforme de trading algorithmique conçue selon les standards des hedge funds institutionnels (Citadel, Two Sigma, Jane Street). Architecture event-driven avec 16 services Docker, validation du risque en temps réel, et support multi-assets.

```
Forex (MT5)  ──┐
Crypto (Binance)─┤─► Redis Streams ──► Strategy Engine ──► Risk Engine ──► Execution
Stocks        ──┘        │                                                      │
                         │                                                      │
                    TimescaleDB ◄────── Portfolio Engine ◄──────── Fill Events ◄┘
                         │
                    FastAPI + Streamlit + Grafana
```

---

## ✨ Fonctionnalités

| Domaine | Détails |
|---|---|
| **Market Data** | MT5 (Forex/CFD), Binance (Crypto), GBM simulation fallback |
| **Stratégies** | Plugin registry, EMA Crossover, extensible via `BaseStrategy` |
| **Risque** | Chain of Responsibility: VaR 99%, CVaR, Drawdown, Daily Loss, Position Limit |
| **Circuit Breaker** | 3 états: CLOSED → OPEN → HALF_OPEN (recovery automatique) |
| **Exécution** | MARKET, TWAP, VWAP · MT5BrokerAdapter |
| **Portfolio** | FIFO/VWAP cost basis · Kelly + Vol-Targeting sizing · Decimal précision |
| **Backtest** | Next-bar-open fill · Walk-Forward Optimization · 15 métriques |
| **ML** | GMM Regime Detection · LightGBM · IsolationForest · MLflow tracking |
| **API** | FastAPI REST + WebSocket (JWT auth) · Prometheus metrics |
| **Dashboard** | Streamlit 6 pages · Grafana provisioning auto |
| **Infrastructure** | 16 services Docker · TimescaleDB hypertables · Redis Streams DLQ |

---

## 🏗️ Architecture

```
trading_platform/
├── core/
│   ├── domain/events.py          # Événements immutables (frozen dataclasses)
│   └── interfaces/__init__.py    # Protocoles PEP 544
├── config/settings.py            # Configuration Pydantic v2
├── infrastructure/
│   ├── container.py              # Composition Root (DI)
│   ├── event_bus/                # Redis Streams + Dead-Letter Queue
│   └── repositories/             # asyncpg + TimescaleDB
├── portfolio_engine/             # P&L Decimal, Kelly+VolTarget
├── risk_engine/                  # 5 validateurs + CircuitBreaker HALF_OPEN
├── execution_engine/             # TWAP/VWAP/MARKET + MT5
├── strategy_engine/              # Plugin registry + EMACrossover
├── backtest_engine/              # WFO + 15 statistiques
├── market_data/                  # MT5 + Binance + GBM
├── ml_engine/                    # GMM + LightGBM + MLflow
├── indicator_engine/             # Kernels Numba JIT
├── monitoring/                   # Prometheus + HealthChecker
├── notification/                 # Telegram + SMTP + Webhook
├── api/                          # FastAPI + WebSocket JWT
├── dashboard/                    # Streamlit dark terminal
├── docker/                       # Dockerfile multi-stage + nginx + Grafana
├── migrations/init.sql           # TimescaleDB schema
└── tests/                        # 42 tests unitaires
```

**Principes appliqués :**
- **Clean Architecture** — domaine sans dépendances externes
- **SOLID** — SRP, OCP, LSP, ISP, DIP stricts
- **Dependency Injection** — composition root unique
- **Chain of Responsibility** — validation du risque
- **Protocol (PEP 544)** — interfaces structurelles
- **Immutabilité** — events `frozen=True, slots=True`

---

## 🚀 Quick Start

### Prérequis

```bash
python --version    # ≥ 3.12
docker --version    # ≥ 24.0
docker compose version  # ≥ 2.20
```

### Installation

```bash
# 1. Cloner le projet
git clone https://github.com/maha-j/algo-trading-platform.git
cd algo-trading-platform

# 2. Configuration
cp .env.example .env
# Éditer .env avec vos paramètres

# 3. Installer les dépendances Python
pip install poetry
poetry install

# 4. Démarrer l'infrastructure
docker compose -f docker/docker-compose.yml up -d postgres redis prometheus grafana

# 5. Migrations base de données
docker exec -i trading-postgres psql -U trading -d trading < migrations/init.sql

# 6. Lancer l'API
poetry run uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 7. Lancer le dashboard (autre terminal)
poetry run streamlit run dashboard/app.py
```

### Docker (stack complète)

```bash
# Générer les certificats TLS
mkdir -p docker/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout docker/ssl/key.pem -out docker/ssl/cert.pem \
    -subj "/CN=trading-platform"

# Lancer tous les services
docker compose -f docker/docker-compose.yml up -d --build
```

---

## 🔐 Configuration minimale (`.env`)

```dotenv
ENVIRONMENT=development

DB_HOST=localhost
DB_PASSWORD=your_strong_password

REDIS_HOST=localhost

# ⚠️ Obligatoire en production (min 32 caractères)
API_SECRET_KEY=change_this_in_production_min_32_chars

RISK_MAX_POSITION_SIZE_PCT=0.05
RISK_MAX_DAILY_LOSS_PCT=0.03
RISK_MAX_DRAWDOWN_PCT=0.15
```

---

## 🧪 Testing

```bash
# Suite complète (42 tests)
poetry run pytest tests/unit/ -v

# Par catégorie
poetry run pytest tests/unit/test_core.py::TestPortfolioEngine -v
poetry run pytest tests/unit/test_core.py::TestRiskEngine -v
poetry run pytest tests/unit/test_core.py::TestDomainEvents -v

# Avec couverture
poetry run pytest tests/unit/ --cov=. --cov-report=html
```

**Résultats :**
```
42 passed in 1.59s
├── TestDomainEvents        (10 tests) ✅
├── TestPortfolioEngine     (8 tests)  ✅
├── TestRiskEngine          (8 tests)  ✅
├── TestRedisEventBus       (4 tests)  ✅
├── TestSettings            (4 tests)  ✅
├── TestPositionSizer       (5 tests)  ✅
└── TestInterfaceCompliance (3 tests)  ✅
```

---

## 📊 Interfaces

| Interface | URL | Description |
|---|---|---|
| API REST | http://localhost:8000/api/docs | FastAPI Swagger UI |
| Dashboard | http://localhost:8501 | Streamlit monitoring |
| Prometheus | http://localhost:9090 | Métriques temps réel |
| Grafana | http://localhost:3000 | Dashboards (admin/admin123) |
| MLflow | http://localhost:5000 | ML experiment tracking |

---

## 🛡️ Gestion du risque

Le Risk Engine implémente 5 validateurs en chaîne (Chain of Responsibility) :

```
Signal ──► PositionLimitValidator  (max 5% equity/position)
       ──► MaxOpenPositionsValidator (max 20 positions)
       ──► DailyLossValidator       (arrêt sur -3% jour)
       ──► VaRValidator             (VaR 99% ≤ 2%)
       ──► DrawdownValidator        (flatten sur -15% drawdown)
              │
              ▼
         CircuitBreaker
         CLOSED → OPEN → HALF_OPEN → CLOSED
```

---

## 🔄 Flux d'événements

```
[Market Data] ──XADD──► stream:bars
     │
     ▼
[Strategy Engine] ──► stream:signals
     │
     ▼
[Risk Engine] ──► stream:risk (breaches → DLQ)
     │
     ▼
[Execution Engine] ──► stream:orders ──► stream:fills
     │
     ▼
[Portfolio Engine] ──► equity curve, P&L, drawdown
```

---

## 📦 Stack technique

```
Python 3.12        FastAPI 0.110      Redis 7.2
TimescaleDB 2.14   asyncpg 0.29       Pydantic v2
Numba 0.59         LightGBM 4.3       MLflow 2.11
Streamlit 1.32     Prometheus         Grafana 10
Docker Compose     nginx 1.25         pytest-asyncio
```

---

## 📄 Licence

MIT License — voir [LICENSE](LICENSE)

---

<div align="center">

*Conçu selon les standards de Citadel, Two Sigma, Jane Street et Renaissance Technologies*

**[⬆ Retour en haut](#-algo-trading-platform)**

</div>
