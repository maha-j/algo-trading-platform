-- =============================================================================
-- Institutional Algorithmic Trading Platform
-- TimescaleDB Schema — init.sql
--
-- Conventions:
--   * All timestamps are TIMESTAMPTZ (UTC).
--   * Monetary values stored as NUMERIC(20,8) to match Python Decimal precision.
--   * Hypertables partitioned by time for time-series query efficiency.
--   * Chunk interval: 1 week for OHLCV, 1 day for ticks and fills.
--   * Compression policy: chunks older than 7 days are compressed.
--   * Continuous aggregates (CAGGs) pre-compute daily OHLCV from tick data.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---------------------------------------------------------------------------
-- Instruments (reference data)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    id             SERIAL PRIMARY KEY,
    symbol         VARCHAR(32) NOT NULL UNIQUE,
    asset_class    VARCHAR(16) NOT NULL CHECK (asset_class IN ('FOREX','CRYPTO','STOCKS','FUTURES')),
    base_currency  VARCHAR(8),
    quote_currency VARCHAR(8),
    lot_size       NUMERIC(20,8) DEFAULT 1,
    tick_size      NUMERIC(20,8) DEFAULT 0.00001,
    contract_size  NUMERIC(20,8) DEFAULT 100000,
    exchange       VARCHAR(32),
    is_active      BOOLEAN DEFAULT TRUE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO instruments (symbol, asset_class, base_currency, quote_currency, lot_size, tick_size, contract_size)
VALUES
    ('EURUSD', 'FOREX',   'EUR', 'USD', 0.01, 0.00001, 100000),
    ('GBPUSD', 'FOREX',   'GBP', 'USD', 0.01, 0.00001, 100000),
    ('USDJPY', 'FOREX',   'USD', 'JPY', 0.01, 0.001,   100000),
    ('BTCUSDT','CRYPTO',  'BTC', 'USDT', 0.001, 0.01,  1),
    ('ETHUSDT','CRYPTO',  'ETH', 'USDT', 0.01,  0.01,  1),
    ('AAPL',   'STOCKS',  'USD', 'USD',  1,     0.01,   1),
    ('SPY',    'STOCKS',  'USD', 'USD',  1,     0.01,   1),
    ('ES1!',   'FUTURES', 'USD', 'USD',  1,     0.25,   50)
ON CONFLICT (symbol) DO NOTHING;

-- ---------------------------------------------------------------------------
-- OHLCV bars (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(32) NOT NULL REFERENCES instruments(symbol),
    timeframe   VARCHAR(4)  NOT NULL,
    open        NUMERIC(20, 8) NOT NULL,
    high        NUMERIC(20, 8) NOT NULL,
    low         NUMERIC(20, 8) NOT NULL,
    close       NUMERIC(20, 8) NOT NULL,
    volume      NUMERIC(20, 8) DEFAULT 0,
    source      VARCHAR(16) DEFAULT 'unknown',
    is_synthetic BOOLEAN DEFAULT FALSE,
    CONSTRAINT ohlcv_pk PRIMARY KEY (time, symbol, timeframe)
);

SELECT create_hypertable('ohlcv', 'time',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- Compression (7-day policy)
ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('ohlcv', INTERVAL '7 days', if_not_exists => TRUE);

-- Indices for common query patterns
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf_time
    ON ohlcv (symbol, timeframe, time DESC);

-- ---------------------------------------------------------------------------
-- Tick data (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticks (
    time    TIMESTAMPTZ NOT NULL,
    symbol  VARCHAR(32) NOT NULL,
    bid     NUMERIC(20, 8) NOT NULL,
    ask     NUMERIC(20, 8) NOT NULL,
    volume  NUMERIC(20, 8) DEFAULT 0,
    source  VARCHAR(16) DEFAULT 'unknown'
);

SELECT create_hypertable('ticks', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

ALTER TABLE ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('ticks', INTERVAL '3 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Signals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_id  VARCHAR(64) NOT NULL,
    symbol       VARCHAR(32) NOT NULL,
    direction    VARCHAR(8)  NOT NULL CHECK (direction IN ('LONG','SHORT','FLAT')),
    strength     NUMERIC(5, 4),
    signal_price NUMERIC(20, 8),
    timeframe    VARCHAR(4),
    event_id     UUID UNIQUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

SELECT create_hypertable('signals', 'time',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_signals_strategy_symbol
    ON signals (strategy_id, symbol, time DESC);

-- ---------------------------------------------------------------------------
-- Orders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id      UUID NOT NULL UNIQUE,
    signal_id     UUID REFERENCES signals(event_id) ON DELETE SET NULL,
    symbol        VARCHAR(32) NOT NULL,
    side          VARCHAR(4)  NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity      NUMERIC(20, 8) NOT NULL CHECK (quantity > 0),
    order_type    VARCHAR(16) NOT NULL,
    algorithm     VARCHAR(16) NOT NULL,
    state         VARCHAR(24) NOT NULL DEFAULT 'PENDING_NEW',
    risk_approved BOOLEAN DEFAULT FALSE,
    submitted_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_state
    ON orders (symbol, state, submitted_at DESC);

-- ---------------------------------------------------------------------------
-- Fills (executions)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    order_id      UUID REFERENCES orders(order_id) ON DELETE SET NULL,
    symbol        VARCHAR(32) NOT NULL,
    side          VARCHAR(4)  NOT NULL,
    quantity      NUMERIC(20, 8) NOT NULL,
    fill_price    NUMERIC(20, 8) NOT NULL,
    commission    NUMERIC(20, 8) DEFAULT 0,
    slippage      NUMERIC(20, 8) DEFAULT 0,
    realised_pnl  NUMERIC(20, 8) DEFAULT 0,
    source        VARCHAR(16) DEFAULT 'unknown'
);

SELECT create_hypertable('fills', 'time',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_fills_symbol_time
    ON fills (symbol, time DESC);

-- ---------------------------------------------------------------------------
-- Portfolio snapshots (equity curve)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    time              TIMESTAMPTZ NOT NULL,
    equity            NUMERIC(20, 8) NOT NULL,
    cash              NUMERIC(20, 8) NOT NULL,
    realised_pnl      NUMERIC(20, 8) DEFAULT 0,
    unrealised_pnl    NUMERIC(20, 8) DEFAULT 0,
    drawdown_pct      NUMERIC(8, 4)  DEFAULT 0,
    open_positions    INTEGER DEFAULT 0,
    exposure_pct      NUMERIC(8, 4)  DEFAULT 0
);

SELECT create_hypertable('portfolio_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- ---------------------------------------------------------------------------
-- Risk events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_events (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    breach_type   VARCHAR(32) NOT NULL,
    current_value NUMERIC(20, 8),
    limit_value   NUMERIC(20, 8),
    action        VARCHAR(32),
    resolved_at   TIMESTAMPTZ,
    notes         TEXT
);

SELECT create_hypertable('risk_events', 'time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- ---------------------------------------------------------------------------
-- ML model registry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ml_models (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_id      VARCHAR(64) NOT NULL,
    symbol        VARCHAR(32) NOT NULL,
    model_type    VARCHAR(32) NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    trained_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics       JSONB,
    artifact_path TEXT,
    is_active     BOOLEAN DEFAULT FALSE,
    UNIQUE (model_id, symbol, version)
);

-- ---------------------------------------------------------------------------
-- Continuous aggregates — daily OHLCV from hourly bars
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)    AS day,
    symbol,
    FIRST(open,  time)            AS open,
    MAX(high)                     AS high,
    MIN(low)                      AS low,
    LAST(close,  time)            AS close,
    SUM(volume)                   AS volume
FROM ohlcv
WHERE timeframe = 'H1'
GROUP BY day, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_daily',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ---------------------------------------------------------------------------
-- Audit log (immutable append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL,
    time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor      VARCHAR(64),
    action     VARCHAR(64) NOT NULL,
    resource   VARCHAR(64),
    resource_id VARCHAR(128),
    details    JSONB,
    ip_address INET
);

SELECT create_hypertable('audit_log', 'time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Prevent updates/deletes on audit log (append-only policy)
CREATE OR REPLACE RULE audit_no_update AS
    ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE OR REPLACE RULE audit_no_delete AS
    ON DELETE TO audit_log DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------------
-- Notification log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_log (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel    VARCHAR(32) NOT NULL,
    level      VARCHAR(16) NOT NULL,
    subject    TEXT NOT NULL,
    delivered  BOOLEAN DEFAULT FALSE,
    error      TEXT
);

-- ---------------------------------------------------------------------------
-- Useful views
-- ---------------------------------------------------------------------------

-- Open P&L per symbol (latest fill price vs avg entry)
CREATE OR REPLACE VIEW v_open_positions AS
SELECT
    f.symbol,
    SUM(CASE WHEN f.side='BUY' THEN f.quantity ELSE -f.quantity END) AS net_qty,
    AVG(f.fill_price) AS avg_entry,
    SUM(f.realised_pnl) AS realised_pnl,
    MAX(f.time) AS last_fill_at
FROM fills f
GROUP BY f.symbol
HAVING SUM(CASE WHEN f.side='BUY' THEN f.quantity ELSE -f.quantity END) != 0;

-- Daily P&L summary
CREATE OR REPLACE VIEW v_daily_pnl AS
SELECT
    time_bucket('1 day', time) AS day,
    SUM(realised_pnl)          AS daily_pnl,
    SUM(commission)            AS daily_commission,
    COUNT(*)                   AS fill_count
FROM fills
GROUP BY day
ORDER BY day DESC;
