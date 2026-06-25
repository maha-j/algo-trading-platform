# Institutional Algorithmic Trading Platform — Complete Structure

trading_platform/
├── .env.example
├── .env.development
├── docker-compose.yml
├── docker-compose.override.yml          # dev overrides
├── Makefile
├── pyproject.toml
├── README.md
│
├── config/
│   ├── __init__.py
│   ├── settings.py                       # Pydantic BaseSettings
│   ├── logging_config.py                 # structlog config
│   └── constants.py
│
├── core/                                 # Domain layer (no deps on infra)
│   ├── __init__.py
│   ├── interfaces/
│   │   ├── __init__.py
│   │   ├── i_data_provider.py
│   │   ├── i_indicator.py
│   │   ├── i_strategy.py
│   │   ├── i_risk_engine.py
│   │   ├── i_execution_engine.py
│   │   ├── i_portfolio_engine.py
│   │   ├── i_backtest_engine.py
│   │   ├── i_ml_model.py
│   │   ├── i_notification.py
│   │   └── i_repository.py
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── events.py                     # BaseEvent + all domain events
│   │   ├── models.py                     # Bar, Tick, Order, Fill, Position
│   │   ├── enums.py                      # OrderSide, OrderType, AssetClass
│   │   └── value_objects.py              # Price, Quantity, Symbol
│   └── exceptions.py
│
├── market_data/                          # Layer 1
│   ├── __init__.py
│   ├── service.py                        # MarketDataService
│   ├── normalizer.py                     # Raw → domain model
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── mt5_provider.py
│   │   ├── binance_provider.py
│   │   ├── ib_provider.py
│   │   └── csv_provider.py              # for backtesting
│   ├── storage/
│   │   ├── ohlcv_repository.py
│   │   └── tick_repository.py
│   └── tests/
│       ├── test_mt5_provider.py
│       └── test_normalizer.py
│
├── indicator_engine/                     # Layer 2
│   ├── __init__.py
│   ├── service.py                        # IndicatorService
│   ├── registry.py                       # Plugin registry
│   ├── indicators/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── trend/
│   │   │   ├── ema.py
│   │   │   ├── sma.py
│   │   │   └── macd.py
│   │   ├── momentum/
│   │   │   ├── rsi.py
│   │   │   ├── stochastic.py
│   │   │   └── cci.py
│   │   ├── volatility/
│   │   │   ├── atr.py
│   │   │   ├── bollinger_bands.py
│   │   │   └── keltner_channel.py
│   │   └── volume/
│   │       ├── vwap.py
│   │       └── obv.py
│   └── tests/
│       └── test_indicators.py
│
├── strategy_engine/                      # Layer 3
│   ├── __init__.py
│   ├── service.py                        # StrategyService
│   ├── registry.py
│   ├── signal_bus.py
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base_strategy.py
│   │   ├── trend_following/
│   │   │   ├── ema_crossover.py
│   │   │   └── breakout.py
│   │   ├── mean_reversion/
│   │   │   ├── pairs_trading.py
│   │   │   └── rsi_reversion.py
│   │   ├── arbitrage/
│   │   │   └── stat_arb.py
│   │   └── ml_based/
│   │       └── ml_strategy.py
│   └── tests/
│       └── test_strategies.py
│
├── portfolio_engine/                     # Layer 4
│   ├── __init__.py
│   ├── service.py                        # PortfolioService
│   ├── position_manager.py
│   ├── sizing/
│   │   ├── __init__.py
│   │   ├── kelly_criterion.py
│   │   ├── fixed_fraction.py
│   │   └── volatility_targeting.py
│   ├── rebalancing.py
│   ├── correlation_engine.py
│   └── tests/
│       └── test_portfolio.py
│
├── risk_engine/                          # Layer 5
│   ├── __init__.py
│   ├── service.py                        # RiskService
│   ├── validators.py                     # Chain of responsibility
│   ├── metrics/
│   │   ├── __init__.py
│   │   ├── var_calculator.py            # VaR / CVaR
│   │   ├── drawdown_monitor.py
│   │   └── exposure_calculator.py
│   ├── circuit_breaker.py
│   ├── limits.py                        # Position / daily loss limits
│   └── tests/
│       └── test_risk.py
│
├── execution_engine/                     # Layer 6
│   ├── __init__.py
│   ├── service.py                        # ExecutionService
│   ├── order_router.py                  # Smart order routing
│   ├── algorithms/
│   │   ├── __init__.py
│   │   ├── twap.py
│   │   ├── vwap.py
│   │   ├── pov.py                       # Percent of volume
│   │   └── iceberg.py
│   ├── slippage_model.py
│   ├── fill_simulator.py                # for backtesting
│   ├── order_book.py
│   └── tests/
│       └── test_execution.py
│
├── backtest_engine/                      # Layer 7
│   ├── __init__.py
│   ├── service.py                        # BacktestService
│   ├── event_engine.py                  # Event-driven loop
│   ├── data_handler.py
│   ├── statistics/
│   │   ├── __init__.py
│   │   ├── performance.py               # Sharpe, Sortino, Calmar
│   │   ├── tearsheet.py
│   │   └── monte_carlo.py
│   ├── walk_forward.py
│   ├── optimization/
│   │   ├── __init__.py
│   │   ├── grid_search.py
│   │   └── bayesian_opt.py
│   └── tests/
│       └── test_backtest.py
│
├── ml_engine/                            # Layer 8
│   ├── __init__.py
│   ├── service.py                        # MLService
│   ├── feature_engineering.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── base_model.py
│   │   ├── regime_classifier.py         # HMM / RandomForest
│   │   ├── return_predictor.py          # LSTM / Transformer
│   │   └── volatility_forecaster.py     # GARCH / NN
│   ├── training/
│   │   ├── pipeline.py
│   │   ├── cross_validation.py          # TimeSeriesSplit
│   │   └── hyperopt.py
│   ├── mlflow_tracker.py
│   └── tests/
│       └── test_ml.py
│
├── monitoring/                           # Layer 9
│   ├── __init__.py
│   ├── service.py
│   ├── metrics_registry.py              # Prometheus metrics
│   ├── health_checks.py
│   ├── alerting.py
│   └── dashboards/
│       └── grafana_provisioning/
│           ├── datasources.yml
│           └── dashboards/
│               ├── trading_overview.json
│               └── risk_dashboard.json
│
├── notification/                         # Layer 11
│   ├── __init__.py
│   ├── service.py                        # NotificationService
│   ├── channels/
│   │   ├── __init__.py
│   │   ├── telegram.py
│   │   ├── email.py
│   │   └── webhook.py
│   ├── templates/
│   │   ├── fill_notification.j2
│   │   └── risk_alert.j2
│   └── tests/
│       └── test_notifications.py
│
├── api/                                  # FastAPI layer
│   ├── __init__.py
│   ├── main.py
│   ├── dependencies.py                  # DI injection
│   ├── middleware/
│   │   ├── auth.py
│   │   ├── rate_limit.py
│   │   └── logging.py
│   ├── routers/
│   │   ├── market_data.py
│   │   ├── strategies.py
│   │   ├── portfolio.py
│   │   ├── orders.py
│   │   ├── risk.py
│   │   ├── backtest.py
│   │   └── monitoring.py
│   ├── schemas/
│   │   ├── requests.py
│   │   └── responses.py
│   └── websockets/
│       ├── market_feed.py
│       └── portfolio_feed.py
│
├── dashboard/                            # Layer 10 — Streamlit
│   ├── __init__.py
│   ├── app.py                           # main entrypoint
│   ├── pages/
│   │   ├── 01_overview.py
│   │   ├── 02_live_trading.py
│   │   ├── 03_portfolio.py
│   │   ├── 04_risk.py
│   │   ├── 05_backtest.py
│   │   └── 06_ml_models.py
│   └── components/
│       ├── charts.py
│       └── metrics_cards.py
│
├── infrastructure/                       # Layer — infra adapters
│   ├── __init__.py
│   ├── event_bus/
│   │   ├── __init__.py
│   │   ├── redis_event_bus.py
│   │   └── in_memory_event_bus.py       # for testing
│   ├── database/
│   │   ├── __init__.py
│   │   ├── connection.py
│   │   ├── migrations/
│   │   └── repositories/
│   │       ├── order_repository.py
│   │       └── position_repository.py
│   ├── cache/
│   │   └── redis_cache.py
│   └── container.py                     # DI container wiring
│
├── shared/                              # Cross-cutting concerns
│   ├── __init__.py
│   ├── logging.py                       # structlog setup
│   ├── decorators.py                    # retry, circuit_breaker, timed
│   ├── utils/
│   │   ├── time_utils.py
│   │   ├── math_utils.py
│   │   └── serialization.py
│   └── types.py                         # Type aliases
│
├── tests/                               # Top-level integration tests
│   ├── conftest.py
│   ├── integration/
│   │   ├── test_full_trade_cycle.py
│   │   └── test_backtest_pipeline.py
│   └── fixtures/
│       ├── sample_ohlcv.parquet
│       └── mock_providers.py
│
├── scripts/
│   ├── seed_historical_data.py
│   ├── run_backtest.py
│   └── health_check.py
│
└── docker/
    ├── trading-core/Dockerfile
    ├── api/Dockerfile
    ├── dashboard/Dockerfile
    └── nginx/nginx.conf
