# =============================================================================
# Trading Platform — Developer Makefile
# =============================================================================

.PHONY: all install lint type-check test test-unit test-integration test-bench \
        docker-up docker-down db-migrate coverage clean help

PYTHON := python3.12
PYTEST  := python -m pytest
RUFF    := python -m ruff
MYPY    := python -m mypy

# Default target
all: lint type-check test-unit

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
install:
	@echo ">>> Installing dependencies..."
	pip install -e ".[dev,broker-binance]"
	pre-commit install

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------
lint:
	@echo ">>> Linting with ruff..."
	$(RUFF) check . --fix

format:
	@echo ">>> Formatting..."
	$(RUFF) format .

type-check:
	@echo ">>> Type checking with mypy..."
	$(MYPY) core/ indicator_engine/ portfolio_engine/ risk_engine/ \
	         execution_engine/ backtest_engine/ --ignore-missing-imports

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test: test-unit test-integration

test-unit:
	@echo ">>> Running unit tests..."
	$(PYTEST) tests/unit -m "not slow" -v --tb=short -q

test-integration:
	@echo ">>> Running integration tests (requires Redis)..."
	$(PYTEST) tests/integration -m integration -v --tb=short

test-bench:
	@echo ">>> Running performance benchmarks..."
	$(PYTEST) tests/unit/test_benchmarks.py -v -s --tb=short

test-all:
	@echo ">>> Running ALL tests..."
	$(PYTEST) tests/ -v --tb=short

coverage:
	@echo ">>> Coverage report..."
	$(PYTEST) tests/unit --cov=. --cov-report=html --cov-report=term-missing
	@echo ">>> Open htmlcov/index.html to view coverage"

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-up:
	@echo ">>> Starting all services..."
	cd docker && docker compose up -d
	@echo ">>> Services started. Access:"
	@echo "    API:       http://localhost:8000/docs"
	@echo "    Dashboard: http://localhost:8501"
	@echo "    Grafana:   http://localhost:3000"
	@echo "    Prometheus:http://localhost:9090"

docker-down:
	@echo ">>> Stopping all services..."
	cd docker && docker compose down

docker-logs:
	cd docker && docker compose logs -f trading-core fastapi

docker-restart:
	cd docker && docker compose restart trading-core fastapi

docker-build:
	@echo ">>> Building Docker images..."
	docker build -f docker/Dockerfile --target production -t trading-platform:latest .

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
db-migrate:
	@echo ">>> Running database migrations..."
	PGPASSWORD=${DB_PASSWORD:-strongpassword123} psql \
		-h ${DB_HOST:-localhost} \
		-U ${DB_USER:-trading} \
		-d ${DB_NAME:-trading} \
		-f migrations/init.sql
	@echo ">>> Migration complete"

db-shell:
	PGPASSWORD=${DB_PASSWORD:-strongpassword123} psql \
		-h ${DB_HOST:-localhost} \
		-U ${DB_USER:-trading} \
		-d ${DB_NAME:-trading}

redis-cli:
	redis-cli -h ${REDIS_HOST:-localhost} -a ${REDIS_PASSWORD:-redispassword}

# ---------------------------------------------------------------------------
# Development shortcuts
# ---------------------------------------------------------------------------
run-api:
	@echo ">>> Starting FastAPI server..."
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload --loop uvloop

run-dashboard:
	@echo ">>> Starting Streamlit dashboard..."
	streamlit run dashboard/app.py --server.port 8501

generate-jwt:
	@echo ">>> Generating JWT token for development..."
	$(PYTHON) -c "
from jose import jwt
import time
token = jwt.encode(
    {'sub': 'dev_user', 'role': 'admin', 'exp': int(time.time()) + 86400},
    '$(shell grep JWT_SECRET .env | cut -d= -f2)',
    algorithm='HS256'
)
print('Bearer', token)
"

# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
clean:
	@echo ">>> Cleaning build artifacts..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache"   -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf htmlcov/ .coverage dist/ build/
	@echo ">>> Clean complete"

help:
	@echo ""
	@echo "Trading Platform — Developer Commands"
	@echo "======================================"
	@echo "  make install        Install all dependencies"
	@echo "  make lint           Lint with ruff"
	@echo "  make type-check     Type check with mypy"
	@echo "  make test-unit      Run unit tests"
	@echo "  make test-integration Run integration tests (need Redis)"
	@echo "  make test-bench     Run performance benchmarks"
	@echo "  make coverage       Generate HTML coverage report"
	@echo "  make docker-up      Start all services via Docker Compose"
	@echo "  make docker-down    Stop all services"
	@echo "  make db-migrate     Run TimescaleDB schema migrations"
	@echo "  make run-api        Start FastAPI in dev mode"
	@echo "  make run-dashboard  Start Streamlit dashboard"
	@echo "  make clean          Remove build artifacts"
	@echo ""
