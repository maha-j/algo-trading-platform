# 09 — Déploiement

> **Niveau** : DevOps Engineers, Infrastructure  
> **Fichiers** : `docker/Dockerfile`, `docker/docker-compose.yml`, `Makefile`, `.env.example`

---

## Table des matières

1. [Vue d'ensemble de l'infrastructure](#1-vue-densemble-de-linfrastructure)
2. [Dockerfile multi-stage](#2-dockerfile-multi-stage)
3. [Docker Compose — 16 services](#3-docker-compose--16-services)
4. [Variables d'environnement et secrets](#4-variables-denvironnement-et-secrets)
5. [Démarrage de la plateforme](#5-démarrage-de-la-plateforme)
6. [CI/CD Pipeline](#6-cicd-pipeline)
7. [Scaling et haute disponibilité](#7-scaling-et-haute-disponibilité)
8. [Migrations de base de données](#8-migrations-de-base-de-données)
9. [Gestion des mises à jour (rolling updates)](#9-gestion-des-mises-à-jour-rolling-updates)
10. [Considérations de production](#10-considérations-de-production)

---

## 1. Vue d'ensemble de l'infrastructure

```
┌─────────────────────────────────────────────────────────────────────┐
│                         RÉSEAU TRADING (bridge)                      │
│                         172.20.0.0/16                                │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                    CORE ENGINES                              │    │
│  │                                                              │    │
│  │   trading-core:8001    fastapi:8000    streamlit:8501        │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────┐   ┌────────────────────────────────────┐    │
│  │   INFRASTRUCTURE    │   │         OBSERVABILITÉ              │    │
│  │                     │   │                                    │    │
│  │  postgres:5432      │   │  prometheus:9090  grafana:3000     │    │
│  │  redis:6379         │   │  loki:3100        jaeger:16686     │    │
│  │  mlflow:5000        │   │  alertmanager:9093 promtail        │    │
│  └─────────────────────┘   │  node-exporter:9100               │    │
│                            └────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
          │
          │ (ports exposés à l'hôte)
     ┌────┴────┐
     │  nginx  │  ← Reverse proxy + TLS
     │ :80/443 │
     └─────────┘
```

### Ports exposés à l'hôte

| Service | Port hôte | Notes |
|---|---|---|
| nginx | 80, 443 | Seul accès externe |
| Streamlit | 8501 | Dashboard (loopback recommandé) |
| Prometheus | 9090 | Loopback seulement |
| Grafana | 3000 | Loopback seulement |
| Jaeger UI | 16686 | Loopback seulement |
| PostgreSQL | 5432 | Loopback seulement |
| Redis | 6379 | Loopback seulement |

---

## 2. Dockerfile multi-stage

Le Dockerfile utilise **3 stages** pour minimiser la taille de l'image de production.

### Stage 1 — builder (jamais déployé)

```dockerfile
FROM python:3.12-slim-bookworm AS builder

# Outils de compilation uniquement dans ce stage
RUN apt-get install -y build-essential gcc g++ libpq-dev

# Virtualenv isolé
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Installation de toutes les dépendances
COPY pyproject.toml .
RUN pip install -e ".[broker-mt5,broker-binance]"
```

### Stage 2 — production (base partagée)

```dockerfile
FROM python:3.12-slim-bookworm AS production

# Runtime uniquement — pas de compilateurs
RUN apt-get install -y libpq5 curl

# Utilisateur non-root obligatoire
RUN groupadd -r trading --gid=1001 && \
    useradd -r -g trading --uid=1001 trading

# Copier UNIQUEMENT le virtualenv compilé (pas les outils de build)
COPY --from=builder /opt/venv /opt/venv

# Pre-warm Numba JIT (élimine la latence au premier appel)
RUN python -c "
from indicator_engine.service import _ema_kernel, _rsi_kernel, _atr_kernel
import numpy as np
arr = np.random.randn(200).astype(np.float64)
_ema_kernel(arr, 20); _rsi_kernel(arr, 14); _atr_kernel(arr, arr, arr, 14)
"

USER trading
```

### Stage 3a — core engine

```dockerfile
FROM production AS core
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "api.main:app",
     "--host", "0.0.0.0", "--port", "8001",
     "--workers", "1",          # 1 worker = 1 event loop = pas de race conditions
     "--loop", "uvloop",        # 2× plus rapide qu'asyncio natif
     "--http", "httptools"]     # parser HTTP haute performance
```

### Stage 3b — API gateway

```dockerfile
FROM production AS api
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.main:app",
     "--host", "0.0.0.0", "--port", "8000",
     "--workers", "4"]          # 4 workers pour l'API stateless
```

### Stage 3c — Dashboard

```dockerfile
FROM production AS dashboard
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "dashboard/app.py",
     "--server.port=8501",
     "--server.address=0.0.0.0",
     "--server.headless=true"]
```

### Construction de l'image

```bash
# Image de production
docker build -f docker/Dockerfile --target production -t trading-platform:latest .

# Image spécifique API
docker build -f docker/Dockerfile --target api -t trading-api:latest .

# Taille des images
docker images | grep trading
# trading-platform    latest    847MB   # builder: ~2.3GB, prod: ~847MB
```

---

## 3. Docker Compose — 16 services

### Groupes de services

```yaml
# Démarrer uniquement l'infrastructure (sans les engines)
docker compose up postgres redis -d

# Démarrer tout sauf l'observabilité
docker compose up postgres redis trading-core fastapi streamlit -d

# Démarrer tout
docker compose up -d
```

### Dépendances et ordre de démarrage

```
postgres (health: pg_isready)
    └── redis (health: redis-cli ping)
           └── trading-core (depends_on: postgres+redis healthy)
                  └── fastapi (depends_on: trading-core)
                         └── nginx (depends_on: fastapi)
                         └── streamlit (depends_on: fastapi)
```

### Limites de ressources

```yaml
trading-core:
  deploy:
    resources:
      limits:
        cpus:   "4"     # 4 cœurs pour le trading + ML
        memory: 8G      # Indicateurs Numba + historique en mémoire

fastapi:
  deploy:
    resources:
      limits:
        cpus:   "2"
        memory: 2G

postgres:
  deploy:
    resources:
      limits:
        cpus:   "2"
        memory: 4G      # TimescaleDB a besoin de mémoire pour la compression
```

### Volumes nommés

```yaml
volumes:
  postgres_data:   # Données TimescaleDB — JAMAIS effacer en prod
  redis_data:      # Streams Redis (durabilité AOF)
  prometheus_data: # Métriques 90 jours
  grafana_data:    # Dashboards et config
  loki_data:       # Logs indexés
```

---

## 4. Variables d'environnement et secrets

### Structure `.env` (ne jamais commiter)

```bash
# Copier .env.example et remplir
cp .env.example .env

# Générer un JWT secret fort
openssl rand -hex 32

# Générer un mot de passe DB fort
openssl rand -base64 32
```

### Variables requises

| Variable | Requis | Exemple |
|---|---|---|
| `DB_PASSWORD` | ✅ | `9xKq3...` |
| `REDIS_PASSWORD` | ✅ | `rEdIs4...` |
| `JWT_SECRET` | ✅ | `openssl rand -hex 32` |
| `MT5_LOGIN` | ⚠️ (si MT5) | `12345678` |
| `MT5_PASSWORD` | ⚠️ (si MT5) | |
| `MT5_SERVER` | ⚠️ (si MT5) | `ICMarkets-Demo` |
| `TELEGRAM_TOKEN` | ❌ (optionnel) | `123:abc...` |
| `SLACK_WEBHOOK_URL` | ❌ (optionnel) | `https://hooks.slack...` |

### Secrets en production (Docker Secrets)

```yaml
# docker-compose.prod.yml — pour la production
secrets:
  db_password:
    external: true    # Créé avec: docker secret create db_password -

services:
  postgres:
    secrets: [db_password]
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
```

```bash
# Créer les secrets Docker
echo "strongpassword" | docker secret create db_password -
echo "jwtSecret..."   | docker secret create jwt_secret -
```

---

## 5. Démarrage de la plateforme

### Démarrage complet depuis zéro

```bash
# 1. Cloner le projet
git clone https://github.com/yourorg/trading-platform.git
cd trading-platform

# 2. Configurer l'environnement
cp .env.example .env
# Éditer .env avec les vraies valeurs

# 3. Construire les images
docker compose build

# 4. Démarrer l'infrastructure
docker compose up postgres redis -d

# 5. Attendre que PostgreSQL soit prêt (healthcheck)
docker compose ps | grep healthy

# 6. Appliquer les migrations
make db-migrate

# 7. Démarrer tous les services
docker compose up -d

# 8. Vérifier la santé
curl http://localhost:8000/health/detailed
```

### Vérification post-démarrage

```bash
# Tous les services démarrés
docker compose ps

# Logs trading core
docker compose logs trading-core --tail 50 -f

# Santé de la plateforme
curl http://localhost:8000/health/detailed | python -m json.tool

# Métriques Prometheus
curl http://localhost:8001/metrics | grep trading_platform_uptime

# Interface Grafana
open http://localhost:3000   # admin / admin123
```

### Commandes Makefile

```bash
make docker-up       # démarrer tout
make docker-down     # tout arrêter
make docker-logs     # logs en direct
make docker-restart  # redémarrer les engines
make db-migrate      # migrations SQL
make db-shell        # psql interactif
make redis-cli       # redis-cli interactif
make run-api         # FastAPI en mode dev (reload)
make run-dashboard   # Streamlit seul
```

---

## 6. CI/CD Pipeline

### GitHub Actions — `.github/workflows/ci.yml`

```yaml
name: CI

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python 3.12
        uses: actions/setup-python@v4
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Lint
        run: ruff check .

      - name: Type check
        run: mypy core/ indicator_engine/ portfolio_engine/ risk_engine/

      - name: Unit tests
        run: pytest tests/unit -m "not slow" --tb=short

      - name: Integration tests
        run: pytest tests/integration -m integration --tb=short
        env:
          REDIS_HOST: localhost
          REDIS_PASSWORD: ""

      - name: Coverage report
        run: pytest tests/unit --cov=. --cov-fail-under=80

  build:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'

    steps:
      - name: Build Docker image
        run: docker build -f docker/Dockerfile --target production -t trading-platform:${{ github.sha }} .

      - name: Push to registry
        run: |
          docker tag trading-platform:${{ github.sha }} ghcr.io/yourorg/trading-platform:latest
          docker push ghcr.io/yourorg/trading-platform:latest
```

### Checks obligatoires avant merge

```
✅ Tests unitaires passent (0 failures)
✅ Coverage > 80%
✅ mypy sans erreurs
✅ ruff sans warnings
✅ Build Docker réussi
✅ Tests d'intégration passent (avec Redis)
```

---

## 7. Scaling et haute disponibilité

### Trading Core (stateful — ne pas scaler horizontalement)

```
❌ NE PAS faire :
  trading-core replicas: 3
  → Race conditions sur les positions en mémoire
  → Doublons d'ordres si deux instances reçoivent le même signal

✅ Haute disponibilité via :
  - Restart policy: on-failure / always
  - Health checks Docker
  - État externalisé dans Redis + PostgreSQL
  - Redémarrage automatique < 30 secondes
```

### FastAPI (stateless — scalable)

```yaml
# Scale à 4 replicas avec nginx load balancing
fastapi:
  deploy:
    replicas: 4

nginx:
  upstream:
    servers:
      - fastapi:8000  # Docker résout en round-robin
```

### Redis — haute disponibilité

```yaml
# Redis Sentinel pour la HA
redis-master:
  image: redis:7
  command: redis-server --appendonly yes

redis-replica:
  image: redis:7
  command: redis-server --replicaof redis-master 6379

redis-sentinel:
  image: redis:7
  command: redis-sentinel /etc/redis/sentinel.conf
```

### PostgreSQL — réplication

```yaml
# Streaming replication pour la HA
postgres-primary:
  environment:
    POSTGRES_REPLICATION_MODE: master

postgres-replica:
  environment:
    POSTGRES_REPLICATION_MODE: slave
    POSTGRES_MASTER_HOST:      postgres-primary
```

---

## 8. Migrations de base de données

### Stratégie de migration

```bash
# Première installation
make db-migrate   # Exécute migrations/init.sql

# Nouvelle migration (à partir de v1.1)
# 1. Créer le fichier
cat > migrations/002_add_ml_features.sql << 'EOF'
ALTER TABLE signals ADD COLUMN ml_confidence NUMERIC(5,4);
ALTER TABLE signals ADD COLUMN ml_regime VARCHAR(20);
EOF

# 2. Appliquer
PGPASSWORD=$DB_PASSWORD psql -h localhost -U trading -d trading \
  -f migrations/002_add_ml_features.sql

# 3. Vérifier
psql -h localhost -U trading -d trading \
  -c "\d signals"
```

### Rollback

```sql
-- Toujours prévoir le rollback dans la migration
-- migrations/002_add_ml_features.sql

-- UP
ALTER TABLE signals ADD COLUMN ml_confidence NUMERIC(5,4);

-- DOWN (dans un fichier séparé : 002_rollback.sql)
ALTER TABLE signals DROP COLUMN IF EXISTS ml_confidence;
```

### Migration zéro downtime

```sql
-- 1. Ajouter la colonne nullable (pas de verrou long)
ALTER TABLE fills ADD COLUMN slippage_bps NUMERIC(10,4);

-- 2. Backfill en batches (sans bloquer les écritures)
UPDATE fills
SET slippage_bps = abs(slippage / fill_price * 10000)
WHERE id BETWEEN 1 AND 100000
  AND slippage_bps IS NULL;

-- 3. Ajouter la contrainte NOT NULL après backfill complet
ALTER TABLE fills ALTER COLUMN slippage_bps SET DEFAULT 0;
```

---

## 9. Gestion des mises à jour (rolling updates)

### Séquence de déploiement sans interruption

```bash
# 1. Construire la nouvelle image
docker build -f docker/Dockerfile --target production \
  -t trading-platform:v1.2.0 .

# 2. Tagger
docker tag trading-platform:v1.2.0 trading-platform:latest

# 3. Mettre à jour fastapi en premier (stateless)
docker compose up -d --no-deps fastapi

# 4. Attendre le health check
sleep 10
curl http://localhost:8000/health

# 5. Mettre à jour le core (très rapide, < 30s downtime)
# Note: le trading est interrompu pendant ce temps
docker compose up -d --no-deps trading-core

# 6. Vérifier les logs
docker compose logs trading-core --tail 30
```

### Blue-Green deployment (production)

```
Version A (actuelle) : trading-core-blue:8001
Version B (nouvelle)  : trading-core-green:8002

1. Démarrer Green parallèlement
2. Vérifier Green pendant 5 minutes
3. Basculer nginx vers Green
4. Arrêter Blue après 1 minute (positions en cours fermées)
```

---

## 10. Considérations de production

### Checklist avant mise en production

```
Infrastructure
  ☐ TLS configuré sur nginx (Let's Encrypt ou certificat propre)
  ☐ Firewall : seuls les ports 80/443 accessibles depuis Internet
  ☐ Toutes les connexions internes sur TLS (Redis TLS, PG SSL)
  ☐ Docker Secrets au lieu des variables d'environnement pour les credentials
  ☐ Backups automatiques PostgreSQL (pg_dump quotidien → S3)
  ☐ Monitoring alertes configurées (email + PagerDuty)

Sécurité
  ☐ Rotation des credentials MT5 trimestrielle
  ☐ JWT expiry < 24h
  ☐ Audit log activé (table audit_log)
  ☐ HTTPS forcé (redirection 301 HTTP→HTTPS)

Opérations
  ☐ Runbook des incidents documenté (cf. 08_MONITORING)
  ☐ Contacts d'urgence broker (MT5 support)
  ☐ Procédure de rollback testée
  ☐ Paper trading validé 2 semaines avant live
  ☐ Position sizing réduit à 25% pendant le premier mois live
```

### Ressources serveur recommandées

| Environnement | CPU | RAM | Stockage |
|---|---|---|---|
| Développement | 4 cœurs | 8 GB | 50 GB SSD |
| Staging | 8 cœurs | 16 GB | 100 GB SSD |
| Production | 16 cœurs | 32 GB | 500 GB NVMe |

> **Note** : La quantité de RAM est critique. TimescaleDB + Numba warm-up + historique en mémoire consomment facilement 8-12 GB.

---

*Document précédent → [08_MONITORING.md](08_MONITORING.md)*  
*Document suivant → [10_DASHBOARD.md](10_DASHBOARD.md)*
