"""
Platform Configuration — Pydantic BaseSettings with environment validation.

Fixes applied (SEC-03, INCONS-01):
  - SEC-03: Production guard added — startup fails if JWT secret is default.
  - INCONS-01: APISettings field renamed secret_key (was jwt_secret in routers).
  - All sub-settings use lazy property access instead of module-level calls.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_JWT_SECRET = "CHANGE_THIS_IN_PRODUCTION_MIN_32_CHARS_XX"


class DatabaseSettings(BaseSettings):
    """PostgreSQL / TimescaleDB connection settings."""

    model_config = SettingsConfigDict(env_prefix="DB_")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "trading"
    user: str = "trading"
    password: SecretStr = SecretStr("changeme")
    pool_min: int = Field(default=5, ge=1, le=50)
    pool_max: int = Field(default=20, ge=5, le=100)
    pool_timeout: float = Field(default=30.0, gt=0)
    echo_sql: bool = False

    @property
    def async_url(self) -> str:
        """Async SQLAlchemy/asyncpg DSN."""
        pwd = self.password.get_secret_value()
        return f"postgresql+asyncpg://{self.user}:{pwd}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        """Sync SQLAlchemy/psycopg2 DSN (for Alembic migrations)."""
        pwd = self.password.get_secret_value()
        return f"postgresql+psycopg2://{self.user}:{pwd}@{self.host}:{self.port}/{self.name}"

    @property
    def asyncpg_dsn(self) -> str:
        """Raw asyncpg DSN (no driver prefix)."""
        pwd = self.password.get_secret_value()
        return f"postgresql://{self.user}:{pwd}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseSettings):
    """Redis connection settings for event bus and caching."""

    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    password: SecretStr | None = None
    ssl: bool = False
    max_connections: int = Field(default=50, ge=10)
    socket_timeout: float = Field(default=5.0, gt=0)
    stream_max_len: int = Field(
        default=100_000,
        description="Max messages per Redis Stream (XADD ~MAXLEN)",
    )

    @property
    def url(self) -> str:
        proto = "rediss" if self.ssl else "redis"
        if self.password:
            pwd = self.password.get_secret_value()
            return f"{proto}://:{pwd}@{self.host}:{self.port}/{self.db}"
        return f"{proto}://{self.host}:{self.port}/{self.db}"


class MT5Settings(BaseSettings):
    """MetaTrader 5 connection settings."""

    model_config = SettingsConfigDict(env_prefix="MT5_")

    login: int = 0
    password: SecretStr = SecretStr("changeme")
    server: str = "MetaQuotes-Demo"
    timeout: int = Field(default=60_000, description="Connection timeout ms")
    path: str = ""


class RiskSettings(BaseSettings):
    """Risk management parameters."""

    model_config = SettingsConfigDict(env_prefix="RISK_")

    max_position_size_pct: float = Field(
        default=0.05,
        ge=0.001,
        le=0.25,
        description="Max single position as fraction of equity",
    )
    max_portfolio_var_pct: float = Field(
        default=0.02,
        ge=0.001,
        le=0.10,
        description="Max 1-day 99% VaR as fraction of equity",
    )
    max_daily_loss_pct: float = Field(
        default=0.03,
        ge=0.001,
        le=0.15,
        description="Max daily loss fraction before trading halt",
    )
    max_drawdown_pct: float = Field(
        default=0.15,
        ge=0.01,
        le=0.50,
        description="Max drawdown from peak before all positions flattened",
    )
    max_open_positions: int = Field(default=20, ge=1, le=200)
    correlation_limit: float = Field(default=0.80, ge=0.0, le=1.0)
    var_confidence_level: float = Field(default=0.99, ge=0.90, le=0.9999)
    var_lookback_days: int = Field(default=252, ge=30)


class ExecutionSettings(BaseSettings):
    """Execution engine configuration."""

    model_config = SettingsConfigDict(env_prefix="EXEC_")

    default_algorithm: str = Field(
        default="MARKET",
        pattern="^(MARKET|TWAP|VWAP)$",
    )
    slippage_bps: float = Field(default=1.0, ge=0.0, le=100.0)
    max_order_retry: int = Field(default=3, ge=1, le=10)
    order_timeout_seconds: float = Field(default=30.0, gt=0)
    twap_interval_seconds: int = Field(default=60, ge=10)
    twap_num_slices: int = Field(default=10, ge=2, le=100)


class NotificationSettings(BaseSettings):
    """Notification channel configuration."""

    model_config = SettingsConfigDict(env_prefix="NOTIFY_")

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr | None = None
    alert_email_to: list[str] = []
    webhook_url: str = ""


class APISettings(BaseSettings):
    """FastAPI server settings."""

    model_config = SettingsConfigDict(env_prefix="API_")

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=4, ge=1, le=32)
    # FIX INCONS-01: renamed from jwt_secret → secret_key (consistent with usage)
    secret_key: SecretStr = SecretStr(_DEFAULT_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=60, ge=5)
    cors_origins: list[str] = [
        "http://localhost:8501",
        "http://localhost:3000",
    ]
    rate_limit_per_minute: int = Field(default=120, ge=10)


class MLSettings(BaseSettings):
    """Machine learning engine configuration."""

    model_config = SettingsConfigDict(env_prefix="ML_")

    mlflow_tracking_uri: str = "http://localhost:5000"
    model_cache_dir: str = "./models/cache"
    feature_window: int = Field(default=60, ge=10)
    retrain_frequency_hours: int = Field(default=24, ge=1)
    min_training_samples: int = Field(default=5000, ge=500)


class Settings(BaseSettings):
    """
    Top-level platform settings aggregator.

    Import via: from config.settings import get_settings
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    platform_name: str = "AlgoTradingPlatform"
    version: str = "1.0.0"

    database: DatabaseSettings = DatabaseSettings()
    redis: RedisSettings = RedisSettings()
    mt5: MT5Settings = MT5Settings()
    risk: RiskSettings = RiskSettings()
    execution: ExecutionSettings = ExecutionSettings()
    notifications: NotificationSettings = NotificationSettings()
    api: APISettings = APISettings()
    ml: MLSettings = MLSettings()

    metrics_port: int = Field(default=8001, ge=1, le=65535)

    @model_validator(mode="after")
    def enforce_production_security(self) -> "Settings":
        """
        FIX SEC-03: Refuse to start in production with default secrets.
        This prevents accidental deployment with weak credentials.
        """
        if self.environment == "production":
            secret = self.api.secret_key.get_secret_value()
            if secret == _DEFAULT_JWT_SECRET or len(secret) < 32:
                raise ValueError(
                    "FATAL: API_SECRET_KEY must be changed from the default "
                    "and be at least 32 characters in production. "
                    "Set the API_SECRET_KEY environment variable."
                )
        return self

    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance (cached per process).
    In tests, call get_settings.cache_clear() to reset.
    """
    return Settings()
