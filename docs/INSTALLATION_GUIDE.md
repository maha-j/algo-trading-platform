# Guide d'Installation — Plateforme de Trading Algorithmique Institutionnelle

> **Version**: 1.0.0  
> **Python**: 3.12  
> **OS cibles**: Ubuntu 22.04 LTS / Debian 12 / macOS 13+  
> **Durée estimée**: 30–45 minutes

---

## Table des matières

1. [Prérequis système](#1-prérequis-système)
2. [Clonage et structure du projet](#2-clonage-et-structure-du-projet)
3. [Configuration de l'environnement](#3-configuration-de-lenvironnement)
4. [Installation Python (avec Poetry)](#4-installation-python-avec-poetry)
5. [Démarrage de l'infrastructure (Docker)](#5-démarrage-de-linfrastructure-docker)
6. [Migrations de base de données](#6-migrations-de-base-de-données)
7. [Démarrage de la plateforme](#7-démarrage-de-la-plateforme)
8. [Vérification de l'installation](#8-vérification-de-linstallation)
9. [Premier backtest](#9-premier-backtest)
10. [Accès aux interfaces](#10-accès-aux-interfaces)
11. [Configuration MT5 (optionnel)](#11-configuration-mt5-optionnel)
12. [Résolution des problèmes](#12-résolution-des-problèmes)

---

## 1. Prérequis système

### Matériel minimum
| Composant | Minimum     | Recommandé (prod) |
|-----------|-------------|-------------------|
| CPU       | 4 cœurs     | 8 cœurs+          |
| RAM       | 8 Go        | 32 Go             |
| Disque    | 50 Go SSD   | 500 Go NVMe       |
| Réseau    | 100 Mbps    | 1 Gbps            |

### Logiciels requis

```bash
# Vérifiez les versions installées
python3 --version      # ≥ 3.12.0
docker --version       # ≥ 24.0.0
docker compose version # ≥ 2.20.0
git --version          # ≥ 2.40.0
```

### Installation des dépendances système (Ubuntu/Debian)

```bash
sudo apt-get update && sudo apt-get install -y \
    python3.12 python3.12-dev python3.12-venv \
    docker.io docker-compose-plugin \
    git curl wget build-essential libpq-dev \
    openssl ca-certificates

# Ajouter l'utilisateur au groupe docker
sudo usermod -aG docker $USER
newgrp docker
```

### Installation sur macOS (Homebrew)

```bash
brew update
brew install python@3.12 docker git openssl
brew install --cask docker
open /Applications/Docker.app   # démarrer Docker Desktop
```

---

## 2. Clonage et structure du projet

```bash
git clone https://github.com/your-org/trading-platform.git
cd trading-platform
```

### Structure du projet

```
trading_platform/
├── api/                    # FastAPI REST gateway + WebSocket
│   ├── main.py             # Application factory + lifespan
│   └── routers.py          # Endpoints par domaine
├── backtest_engine/        # Moteur de backtesting event-driven
├── config/
│   └── settings.py         # Configuration Pydantic (env vars)
├── core/
│   ├── domain/
│   │   └── events.py       # Événements immutables (dataclasses frozen)
│   └── interfaces/
│       └── __init__.py     # Protocoles PEP 544
├── dashboard/
│   └── app.py              # Dashboard Streamlit
├── docker/
│   ├── Dockerfile           # Multi-stage: core / api / dashboard
│   ├── docker-compose.yml  # 16 services
│   ├── nginx.conf          # Reverse proxy TLS
│   ├── prometheus.yml      # Scrape config
│   ├── alert_rules.yml     # Alertes Prometheus
│   └── grafana/            # Provisioning Grafana
├── execution_engine/       # TWAP, VWAP, Market orders + MT5
├── indicator_engine/       # Kernels Numba (EMA, RSI, ATR, MACD…)
├── infrastructure/
│   ├── container.py        # Composition Root (DI container)
│   ├── event_bus/
│   │   └── redis_event_bus.py  # Redis Streams + DLQ
│   └── repositories/
│       └── db_repositories.py  # asyncpg + TimescaleDB
├── market_data/            # MT5Provider + BinanceProvider + GBM fallback
├── migrations/
│   └── init.sql            # TimescaleDB schema + hypertables
├── ml_engine/              # GMM RegimeDetector + LightGBM + MLflow
├── monitoring/
│   └── metrics.py          # Prometheus registry + HealthChecker
├── notification/           # Telegram + SMTP + Webhook + circuit breakers
├── portfolio_engine/       # P&L Decimal, Kelly+VolTarget sizer
├── risk_engine/            # Chain of Responsibility + CircuitBreaker
├── shared/
│   └── logging_setup.py    # JSON structured logging
├── strategy_engine/        # Plugin registry + EMACrossover
├── tests/
│   ├── unit/               # 42 tests unitaires
│   └── integration/        # Tests d'intégration
├── .env.example            # Template des variables d'environnement
├── pyproject.toml          # Dépendances pinned
└── Makefile                # Commandes de développement
```

---

## 3. Configuration de l'environnement

### Créer le fichier `.env`

```bash
cp .env.example .env
```

### Éditer `.env` avec vos valeurs

```bash
nano .env   # ou vim .env
```

### Variables obligatoires

```dotenv
# ── Environnement ─────────────────────────────────────────────────────────────
ENVIRONMENT=development   # development | staging | production
DEBUG=false
LOG_LEVEL=INFO

# ── Base de données (TimescaleDB) ──────────────────────────────────────────────
DB_HOST=localhost
DB_PORT=5432
DB_NAME=trading
DB_USER=trading
DB_PASSWORD=votre_mot_de_passe_fort_ici   # ⚠️ changez en production

# ── Redis (Event Bus + Cache) ──────────────────────────────────────────────────
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=votre_redis_password       # ⚠️ changez en production

# ── API Gateway ────────────────────────────────────────────────────────────────
API_SECRET_KEY=votre_jwt_secret_minimum_32_caracteres_AABB  # ⚠️ obligatoire en prod

# ── Limites de risque ─────────────────────────────────────────────────────────
RISK_MAX_POSITION_SIZE_PCT=0.05     # 5% max par position
RISK_MAX_DAILY_LOSS_PCT=0.03        # Arrêt sur -3% quotidien
RISK_MAX_DRAWDOWN_PCT=0.15          # Aplatissement sur -15% drawdown
RISK_MAX_OPEN_POSITIONS=20
```

### Variables optionnelles (brokers et notifications)

```dotenv
# ── MetaTrader 5 (optionnel) ───────────────────────────────────────────────────
MT5_LOGIN=12345678
MT5_PASSWORD=votre_mt5_password
MT5_SERVER=MetaQuotes-Demo

# ── Binance (optionnel) ────────────────────────────────────────────────────────
BINANCE_API_KEY=votre_api_key
BINANCE_SECRET_KEY=votre_secret_key

# ── Notifications ──────────────────────────────────────────────────────────────
NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABCdef...
NOTIFY_TELEGRAM_CHAT_ID=-1001234567890
NOTIFY_SMTP_HOST=smtp.gmail.com
NOTIFY_SMTP_PORT=587
NOTIFY_SMTP_USER=votre@email.com
NOTIFY_SMTP_PASSWORD=votre_app_password
NOTIFY_ALERT_EMAIL_TO=["alerte@email.com"]
NOTIFY_WEBHOOK_URL=https://hooks.slack.com/...

# ── MLflow ────────────────────────────────────────────────────────────────────
MLFLOW_TRACKING_URI=http://localhost:5000

# ── Grafana ────────────────────────────────────────────────────────────────────
GRAFANA_PASSWORD=votre_grafana_password
```

---

## 4. Installation Python (avec Poetry)

### Installer Poetry

```bash
curl -sSL https://install.python-poetry.org | python3.12 -
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
poetry --version   # doit afficher Poetry 1.7+
```

### Créer l'environnement virtuel et installer les dépendances

```bash
cd trading_platform/

# Configurer Poetry pour créer le venv dans le projet
poetry config virtualenvs.in-project true

# Installer toutes les dépendances (incluant les optionnelles)
poetry install --no-interaction

# Activer l'environnement
source .venv/bin/activate   # Linux/macOS
# ou
poetry shell
```

### Vérifier l'installation

```bash
python -c "import fastapi, redis, asyncpg, numba, lightgbm; print('✅ Toutes les dépendances OK')"
python -c "from core.domain.events import BarEvent; b = BarEvent(source='test', symbol='EURUSD'); print('✅ Imports domaine OK')"
```

---

## 5. Démarrage de l'infrastructure (Docker)

### Option A — Infrastructure uniquement (développement)

Lance uniquement les services d'infrastructure : PostgreSQL, Redis, Prometheus, Grafana.
Vous exécutez la plateforme Python localement.

```bash
# Lancer seulement l'infra (recommandé en développement)
docker compose -f docker/docker-compose.yml up -d postgres redis prometheus grafana

# Vérifier que les services sont sains
docker compose -f docker/docker-compose.yml ps

# Logs en temps réel
docker compose -f docker/docker-compose.yml logs -f postgres redis
```

Attendez que les healthchecks soient au vert (environ 30 secondes) :

```
trading-postgres   healthy
trading-redis      healthy
trading-prometheus running
trading-grafana    running
```

### Option B — Stack complète (production)

Lance tous les services dans Docker, y compris la plateforme Python.

```bash
# Générer les certificats TLS auto-signés pour nginx (développement)
mkdir -p docker/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout docker/ssl/key.pem \
    -out docker/ssl/cert.pem \
    -subj "/CN=trading-platform/O=Trading/C=FR"

# Lancer la stack complète
docker compose -f docker/docker-compose.yml up -d --build

# Suivre le démarrage
docker compose -f docker/docker-compose.yml logs -f trading-core trading-api
```

### Commandes Docker utiles

```bash
# Arrêter tous les services
docker compose -f docker/docker-compose.yml down

# Arrêter et supprimer les volumes (⚠️ efface toutes les données)
docker compose -f docker/docker-compose.yml down -v

# Reconstruire une image spécifique
docker compose -f docker/docker-compose.yml build trading-api

# Voir les ressources consommées
docker stats
```

---

## 6. Migrations de base de données

```bash
# S'assurer que PostgreSQL est démarré
docker compose -f docker/docker-compose.yml up -d postgres
sleep 5

# Appliquer le schéma initial (TimescaleDB + hypertables)
docker exec -i trading-postgres psql -U trading -d trading \
    < migrations/init.sql

# Vérifier que les tables sont créées
docker exec trading-postgres psql -U trading -d trading \
    -c "\dt"

# Vérifier les hypertables TimescaleDB
docker exec trading-postgres psql -U trading -d trading \
    -c "SELECT hypertable_name FROM timescaledb_information.hypertables;"
```

Sortie attendue :
```
  hypertable_name  
─────────────────
 bars
 portfolio_snapshots
 fills
(3 rows)
```

---

## 7. Démarrage de la plateforme

### En mode développement (processus locaux)

```bash
# Terminal 1 — API Gateway
poetry run uvicorn api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-level info

# Terminal 2 — Dashboard Streamlit
poetry run streamlit run dashboard/app.py \
    --server.port 8501 \
    --server.address 0.0.0.0

# Terminal 3 — Vérification health
curl http://localhost:8000/health
```

### Avec le Makefile (recommandé)

```bash
# Démarrer l'infra + la plateforme
make dev

# Autres commandes disponibles
make test        # tests unitaires + intégration
make test-unit   # tests unitaires uniquement
make lint        # ruff + mypy
make format      # black + isort
make docker-up   # stack Docker complète
make docker-down # arrêter Docker
make logs        # logs en temps réel
```

---

## 8. Vérification de l'installation

### Tests unitaires

```bash
# Lancer tous les tests (42 tests unitaires)
poetry run pytest tests/unit/ -v --tb=short

# Avec couverture
poetry run pytest tests/unit/ --cov=. --cov-report=term-missing

# Tests spécifiques
poetry run pytest tests/unit/test_core.py::TestPortfolioEngine -v
poetry run pytest tests/unit/test_core.py::TestRiskEngine -v
```

Résultat attendu :
```
tests/unit/test_core.py::TestDomainEvents::test_fill_event_fields_correct   PASSED
tests/unit/test_core.py::TestPortfolioEngine::test_initial_equity_correct   PASSED
tests/unit/test_core.py::TestRiskEngine::test_valid_signal_approved         PASSED
...
42 passed in 8.34s
```

### Vérification des endpoints API

```bash
# Health check basique
curl -s http://localhost:8000/health | python3 -m json.tool

# Health check détaillé
curl -s http://localhost:8000/health/detailed | python3 -m json.tool

# Métriques Prometheus
curl -s http://localhost:8000/metrics | grep "api_http"
```

### Obtenir un token JWT pour tester l'API

```bash
# Générer un token de test (développement uniquement)
python3 - << 'EOF'
import jwt, datetime
from config.settings import get_settings
settings = get_settings()
token = jwt.encode(
    {
        "sub": "test-user",
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    },
    settings.api.secret_key.get_secret_value(),
    algorithm=settings.api.jwt_algorithm,
)
print(f"Bearer token:\n{token}")
EOF
```

### Tester les endpoints protégés

```bash
TOKEN="votre_token_jwt_ici"

# Portfolio summary
curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:8000/api/v1/portfolio/summary | python3 -m json.tool

# Métriques de risque
curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:8000/api/v1/risk/metrics | python3 -m json.tool

# Stratégies enregistrées
curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:8000/api/v1/strategies/ | python3 -m json.tool
```

### Tester le WebSocket (avec wscat)

```bash
# Installer wscat
npm install -g wscat

# Se connecter au WebSocket portfolio (authentifié)
wscat -c "ws://localhost:8000/ws/portfolio?token=$TOKEN"

# Se connecter au WebSocket market data
wscat -c "ws://localhost:8000/ws/market-data/EURUSD?token=$TOKEN"
```

---

## 9. Premier backtest

### Lancer un backtest via l'API

```bash
curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
        "strategy_id": "ema_crossover_v1",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "start_date": "2023-01-01",
        "end_date": "2023-12-31",
        "initial_capital": 100000.0,
        "fast_period": 9,
        "slow_period": 21
    }' \
    http://localhost:8000/api/v1/backtest/run | python3 -m json.tool
```

### Lancer un backtest directement en Python

```python
import asyncio
from backtest_engine.service import BacktestEngine, BacktestConfig
from strategy_engine.service import EMACrossoverStrategy

async def run():
    config = BacktestConfig(
        initial_capital   = 100_000.0,
        commission_per_lot = 7.0,
        slippage_bps      = 1.0,
        risk_free_rate    = 0.05,
    )
    strategy = EMACrossoverStrategy(
        strategy_id = "test",
        config      = {"fast_period": 9, "slow_period": 21, "symbols": ["EURUSD"]},
    )
    engine = BacktestEngine(config=config)
    results = await engine.run(
        strategy  = strategy,
        symbol    = "EURUSD",
        timeframe = "H1",
        start     = "2023-01-01",
        end       = "2023-12-31",
    )
    print(f"Sharpe: {results.stats.sharpe_ratio:.2f}")
    print(f"Return: {results.stats.total_return_pct:.1f}%")
    print(f"MaxDD:  {results.stats.max_drawdown_pct:.1f}%")

asyncio.run(run())
```

---

## 10. Accès aux interfaces

| Interface          | URL                             | Identifiants par défaut |
|--------------------|---------------------------------|-------------------------|
| API REST (docs)    | http://localhost:8000/api/docs  | Token JWT requis        |
| Dashboard Streamlit | http://localhost:8501           | Aucun (dev)             |
| Prometheus         | http://localhost:9090           | Aucun                   |
| Grafana            | http://localhost:3000           | admin / admin123        |
| MLflow             | http://localhost:5000           | Aucun (dev)             |
| PostgreSQL         | localhost:5432                  | trading / changeme      |
| Redis              | localhost:6379                  | (PASSWORD env var)      |

### Configurer Grafana

1. Ouvrir http://localhost:3000 → Login: `admin` / `admin123`
2. Le datasource Prometheus est auto-configuré via provisioning
3. Importer le dashboard `docker/grafana/dashboards/trading.json` (si présent)
4. Ou créer manuellement : `+` → Import → coller l'ID `1860` (Node Exporter)

---

## 11. Configuration MT5 (optionnel)

### Prérequis MT5

MetaTrader 5 ne fonctionne que sur **Windows** (natif) ou **Linux** (via Wine).
Sur macOS, utilisez une VM Windows ou le mode simulation (GBM fallback).

### Installation sur Linux avec Wine

```bash
# Installer Wine
sudo apt-get install -y wine64 winbind

# Télécharger MT5
wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
wine mt5setup.exe

# Installer MetaTrader5 Python
pip install MetaTrader5

# Tester la connexion
python3 -c "
import MetaTrader5 as mt5
if mt5.initialize():
    print('✅ MT5 connecté:', mt5.account_info())
    mt5.shutdown()
else:
    print('❌ Erreur:', mt5.last_error())
"
```

### Configurer les variables MT5 dans `.env`

```dotenv
MT5_LOGIN=12345678             # Votre numéro de compte
MT5_PASSWORD=votre_password    # Mot de passe MT5
MT5_SERVER=ICMarkets-Demo01    # Serveur de votre courtier
MT5_PATH=/opt/mt5/terminal64.exe  # Chemin vers le terminal
```

---

## 12. Résolution des problèmes

### Problème : `ImportError: No module named 'MetaTrader5'`

**Cause** : MT5 n'est disponible que sur Windows.
**Solution** : La plateforme démarre automatiquement en mode simulation (GBM) sans MT5.
```bash
# Vérifier que le mode sim fonctionne
python3 -c "from market_data.service import MarketDataService; print('✅ Mode sim OK')"
```

### Problème : `Connection refused` sur Redis/PostgreSQL

```bash
# Vérifier que les conteneurs sont en cours
docker ps | grep -E "redis|postgres"

# Vérifier les logs
docker logs trading-redis
docker logs trading-postgres

# Tester la connexion Redis manuellement
redis-cli -h localhost -p 6379 -a votre_password ping

# Tester PostgreSQL
psql -h localhost -U trading -d trading -c "SELECT 1"
```

### Problème : `FATAL: API_SECRET_KEY must be changed`

La plateforme refuse de démarrer en mode `production` avec le secret JWT par défaut.
```bash
# Générer un secret fort
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Copier le résultat dans API_SECRET_KEY dans .env
```

### Problème : Tests qui échouent avec `asyncio.run()`

```bash
# Installer pytest-asyncio
pip install pytest-asyncio

# Vérifier pyproject.toml
grep asyncio pyproject.toml
# Doit contenir: asyncio_mode = "auto"
```

### Problème : `OSError: [Errno 98] Address already in use`

```bash
# Trouver et tuer le processus qui utilise le port
fuser -k 8000/tcp   # API
fuser -k 8501/tcp   # Dashboard
fuser -k 6379/tcp   # Redis
```

### Réinitialiser complètement la base de données

```bash
docker compose -f docker/docker-compose.yml down -v
docker compose -f docker/docker-compose.yml up -d postgres
sleep 10
docker exec -i trading-postgres psql -U trading -d trading < migrations/init.sql
```

### Activer les logs DEBUG

```bash
LOG_LEVEL=DEBUG poetry run uvicorn api.main:app --reload
```

---

## Annexe — Variables d'environnement complètes

| Variable                     | Défaut                   | Description                      |
|------------------------------|--------------------------|----------------------------------|
| `ENVIRONMENT`                | `development`            | Environnement d'exécution        |
| `LOG_LEVEL`                  | `INFO`                   | Niveau de log                    |
| `DB_HOST`                    | `localhost`              | Hôte PostgreSQL                  |
| `DB_PORT`                    | `5432`                   | Port PostgreSQL                  |
| `DB_NAME`                    | `trading`                | Nom de la base                   |
| `DB_USER`                    | `trading`                | Utilisateur PostgreSQL           |
| `DB_PASSWORD`                | `changeme`               | ⚠️ Changer en production         |
| `REDIS_HOST`                 | `localhost`              | Hôte Redis                       |
| `REDIS_PORT`                 | `6379`                   | Port Redis                       |
| `REDIS_PASSWORD`             | —                        | Mot de passe Redis               |
| `API_SECRET_KEY`             | *(défaut faible)*        | ⚠️ JWT secret — min 32 chars     |
| `API_PORT`                   | `8000`                   | Port API                         |
| `RISK_MAX_POSITION_SIZE_PCT` | `0.05`                   | 5% max par position              |
| `RISK_MAX_DAILY_LOSS_PCT`    | `0.03`                   | Seuil perte journalière          |
| `RISK_MAX_DRAWDOWN_PCT`      | `0.15`                   | Seuil drawdown global            |
| `RISK_MAX_OPEN_POSITIONS`    | `20`                     | Nombre max de positions ouvertes |
| `MT5_LOGIN`                  | `0`                      | Compte MT5                       |
| `MT5_SERVER`                 | `MetaQuotes-Demo`        | Serveur MT5                      |
| `NOTIFY_TELEGRAM_BOT_TOKEN`  | —                        | Token bot Telegram               |
| `NOTIFY_TELEGRAM_CHAT_ID`    | —                        | Chat ID Telegram                 |
| `MLFLOW_TRACKING_URI`        | `http://localhost:5000`  | URI MLflow                       |
| `GRAFANA_PASSWORD`           | `admin123`               | ⚠️ Changer en production         |
