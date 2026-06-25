# 06 — Backtest Engine

> **Niveau** : Quant Researchers, Financial Engineers  
> **Fichiers** : `backtest_engine/service.py`

---

## Table des matières

1. [Vue d'ensemble et philosophie](#1-vue-densemble-et-philosophie)
2. [Modèle de remplissage next-bar-open](#2-modèle-de-remplissage-next-bar-open)
3. [Architecture event-driven](#3-architecture-event-driven)
4. [Statistiques de performance](#4-statistiques-de-performance)
5. [Walk-Forward Optimization (WFO)](#5-walk-forward-optimization-wfo)
6. [Modèles de coût](#6-modèles-de-coût)
7. [Pièges courants (biais à éviter)](#7-pièges-courants-biais-à-éviter)
8. [Utilisation via l'API](#8-utilisation-via-lapi)
9. [Interprétation des résultats](#9-interprétation-des-résultats)
10. [Limites et considérations](#10-limites-et-considérations)

---

## 1. Vue d'ensemble et philosophie

### Objectif d'un backtest rigoureux

Un backtest est une **simulation historique conditionnelle**. Son but n'est pas de prédire les performances futures, mais de mesurer la **cohérence statistique** d'une stratégie et d'identifier ses conditions de défaillance.

### Pipeline en 4 phases

```
Phase 1 — Hydratation des données
  HistoricalDataManager.get_bars()
  └── TimescaleDB → Cache L1 mémoire

Phase 2 — Précalcul des indicateurs
  IndicatorService.compute_all() sur la série complète
  └── Évite la recomputation barre par barre

Phase 3 — Replay événementiel
  for bar in bars:
    → Exécuter les ordres pending (next-bar-open)
    → Mettre à jour P&L non-réalisé
    → Appeler strategy.on_bar()
    → Si signal → calculer taille → créer ordre pending

Phase 4 — Calcul des statistiques
  compute_statistics(equity_curve, fills)
  └── 15 métriques institutionnelles
```

---

## 2. Modèle de remplissage next-bar-open

C'est la décision de design **la plus critique** pour la validité d'un backtest.

### Pourquoi pas le fill à la clôture du signal ?

```
Barre de signal    Barre suivante
      │                  │
  ┌───┴───┐          ┌───┴───┐
  │       │          │       │
  │   C   │─→ signal │  O    │─→ fill réel
  │       │  "BUY"   │       │
  └───────┘          └───────┘
  t=10h00            t=11h00

Fill à la clôture (MAUVAIS) : remplissage à C, prix connu à la génération du signal
→ Look-ahead bias : on sait le prix de clôture avant qu'il ne se forme
→ Statistiques artificiellement gonflées (Sharpe irréaliste)

Fill à l'ouverture suivante (CORRECT) : remplissage à O de t+1
→ Modélise ce qui se passe réellement : signal génère un ordre MOO
→ Gaps overnight modélisés naturellement
```

### Implémentation

```python
async def run(self, strategy, bars, symbol, timeframe):
    bars_list     = list(bars.iterrows())
    pending_orders = []

    for idx, (ts, row) in enumerate(bars_list):

        # ── PHASE A : Exécuter les ordres de la barre précédente ──
        # Ces ordres ont été générés sur bar[idx-1]
        # Ils sont remplis à l'open de bar[idx] = next-bar-open
        for order in list(pending_orders):
            atr_val = ind_svc.get_last_value(symbol, tf, "ATR_14") or 0.0
            fill    = simulated_execution.execute(order, current_bar, atr_val)
            if fill:
                await portfolio.on_fill(fill)
            pending_orders.remove(order)

        # ── PHASE B : Mettre à jour le P&L non-réalisé ──
        await portfolio.on_tick(TickEvent(bid=row.close, ask=row.close, ...))

        # ── PHASE C : Calculer les indicateurs sur le sous-ensemble ──
        sub_df = bars.iloc[max(0, idx - 499): idx + 1]  # fenêtre glissante
        ind_svc.compute_all(symbol, tf, sub_df)

        # ── PHASE D : Appeler la stratégie ──
        signal = await strategy.on_bar(bar, ind_svc)

        # ── PHASE E : Créer l'ordre (sera exécuté à la prochaine barre) ──
        if signal:
            qty   = portfolio.calculate_position_size(...)
            order = OrderEvent(...)
            pending_orders.append(order)   # execution différée !
```

---

## 3. Architecture event-driven

Le backtest réutilise exactement les mêmes composants que le trading live :

| Composant | Live trading | Backtest |
|---|---|---|
| `PortfolioEngine` | ✅ identique | ✅ identique |
| `IndicatorService` | ✅ identique | ✅ identique |
| `BaseStrategy` | ✅ identique | ✅ identique (après `reset()`) |
| `RiskEngine` | ✅ complet | Simplifié (optionnel) |
| `ExecutionEngine` | MT5BrokerAdapter | `SimulatedExecution` |

**Avantage crucial** : ce qu'on backteste est **le même code** que ce qui tourne en production. Les divergences live/backtest viennent uniquement du modèle d'exécution, pas de la logique de signal.

### `SimulatedExecution`

```python
class SimulatedExecution:
    def execute(self, order, next_bar, atr=0.0) -> Optional[FillEvent]:
        fill_px = next_bar.open   # next-bar-open model

        # Slippage gaussien proportionnel à l'ATR
        if atr > 0:
            slippage = np.random.normal(0, atr * config.slippage_bps / 10_000)
            fill_px += slippage if order.side == "BUY" else -slippage

        # Spread modélisé
        spread_cost = fill_px * config.spread_bps / 10_000
        fill_px    += spread_cost / 2 if order.side == "BUY" else -spread_cost / 2

        # Commission fixe par lot
        commission = config.commission_per_lot * float(order.quantity)

        return FillEvent(
            fill_price = round(fill_px, 5),
            commission = commission,
            slippage   = abs(fill_px - next_bar.open),
            ...
        )
```

---

## 4. Statistiques de performance

### Métriques calculées

```python
@dataclass
class BacktestStats:
    # Rendements
    total_return_pct:        float   # (equity_final / equity_initial - 1) × 100
    annualised_return_pct:   float   # (1 + total_return)^(1/years) - 1
    daily_return_mean:       float   # moyenne des returns journaliers
    daily_return_std:        float   # volatilité journalière

    # Ratios risque/rendement
    sharpe_ratio:            float   # (return - Rf) / std × √252
    sortino_ratio:           float   # (return - Rf) / downside_std × √252
    calmar_ratio:            float   # annual_return / max_drawdown
    omega_ratio:             float   # E[gains] / E[losses] vs threshold

    # Drawdown
    max_drawdown_pct:        float   # pire peak-to-trough
    avg_drawdown_pct:        float   # drawdown moyen
    max_drawdown_duration:   int     # durée max en barres

    # Statistiques trades
    total_trades:            int
    winning_trades:          int
    losing_trades:           int
    win_rate_pct:            float   # % trades gagnants
    profit_factor:           float   # gross_profit / gross_loss
    avg_win:                 float   # gain moyen par trade gagnant
    avg_loss:                float   # perte moyenne par trade perdant
    avg_trade_return:        float
    best_trade:              float
    worst_trade:             float

    # Coûts
    total_commission:        float
    total_slippage:          float
    runtime_seconds:         float
```

### Formules détaillées

#### Sharpe Ratio

```
daily_rf = risk_free_rate / 252   (ex: 0.05 / 252 = 0.000198)
excess   = returns - daily_rf

Sharpe = (mean(excess) / std(returns)) × √252
```

> Un Sharpe > 2.0 est exceptionnel, > 1.5 est bon, > 1.0 est acceptable en HF institutionnel.

#### Sortino Ratio

```
downside = returns[returns < daily_rf]

Sortino = (mean(excess) / std(downside)) × √252
```

> Le Sortino pénalise uniquement la volatilité à la baisse, contrairement au Sharpe. Préféré par de nombreux fonds car il ne pénalise pas la hausse.

#### Calmar Ratio

```
Calmar = annualised_return / abs(max_drawdown_pct)
```

> Un Calmar > 1.0 signifie que le rendement annualisé compense le drawdown historique maximum.

#### Omega Ratio

```
threshold = risk_free_rate
gains  = returns[returns > threshold] - threshold
losses = threshold - returns[returns < threshold]

Omega = sum(gains) / sum(losses)
```

> Omega > 1.0 = le profil de rendement favorise les gains vs pertes. Plus robuste que le Sharpe car il utilise toute la distribution (pas uniquement la variance).

#### Profit Factor

```
Profit Factor = gross_profit / gross_loss

gross_profit = sum(pnl for pnl in trade_pnls if pnl > 0)
gross_loss   = sum(abs(pnl) for pnl in trade_pnls if pnl <= 0)
```

> PF > 1.5 = bon, > 2.0 = excellent, > 3.0 = exceptionnel ou sur-ajusté.

---

## 5. Walk-Forward Optimization (WFO)

### Pourquoi le WFO ?

L'optimisation naive sur l'ensemble des données historiques conduit à **l'overfitting** :

```
Backtest simple (mauvais) :
  ─────────────────────────────────────────
  │        IS (in-sample — OPTIMISÉ)       │
  ─────────────────────────────────────────
  → Sharpe = 3.5 (sur-ajusté)

Walk-Forward (correct) :
  ─────────────────────────────
  │   IS₁ (optim)   │ OOS₁ ↓  │
  ─────────────────────────────
       ─────────────────────────────
       │   IS₂ (optim)   │ OOS₂ ↓  │
       ─────────────────────────────
            ─────────────────────────────
            │   IS₃ (optim)   │ OOS₃ ↓  │
            ─────────────────────────────
  → Sharpe OOS moyen = 1.2 (réaliste)
```

### Anchored WFO (expanding IS window)

La fenêtre In-Sample **s'élargit** à chaque split. La stratégie voit plus de données au fil du temps — adapté aux stratégies trend-following.

```
Split 1: IS=[0:1000]    OOS=[1000:1250]
Split 2: IS=[0:1250]    OOS=[1250:1500]
Split 3: IS=[0:1500]    OOS=[1500:1750]
Split 4: IS=[0:1750]    OOS=[1750:2000]
Split 5: IS=[0:2000]    OOS=[2000:2250]
```

### Code

```python
async def walk_forward(
    self,
    strategy_cls,
    strategy_config,
    bars,
    symbol,
    timeframe = "H1",
    in_sample_bars      = 1000,
    out_of_sample_bars  = 250,
    n_splits            = 5,
) -> dict:

    split_results = []

    for split in range(n_splits):
        is_end    = in_sample_bars + split * out_of_sample_bars
        oos_start = is_end
        oos_end   = oos_start + out_of_sample_bars

        if oos_end > len(bars):
            break

        # Backtest OOS uniquement (en production : optimiser sur IS d'abord)
        oos_bars   = bars.iloc[oos_start:oos_end]
        strategy   = strategy_cls(config=strategy_config)
        oos_result = await self.run(strategy, oos_bars, symbol, timeframe)

        split_results.append({
            "split":    split + 1,
            "sharpe":   oos_result["stats"]["sharpe_ratio"],
            "return":   oos_result["stats"]["total_return_pct"],
            "max_dd":   oos_result["stats"]["max_drawdown_pct"],
            "win_rate": oos_result["stats"]["win_rate_pct"],
        })

    return {
        "n_splits":         len(split_results),
        "avg_oos_sharpe":   np.mean([r["sharpe"] for r in split_results]),
        "avg_oos_return":   np.mean([r["return"] for r in split_results]),
        "consistency_pct":  sum(1 for r in split_results if r["return"] > 0)
                            / len(split_results) * 100,
        "split_results":    split_results,
    }
```

### Indicateurs WFO

| Indicateur | Acceptable | Bon | Excellent |
|---|---|---|---|
| `avg_oos_sharpe` | > 0.5 | > 1.0 | > 1.5 |
| `consistency_pct` (% splits profitables) | > 60% | > 75% | > 85% |
| Ratio IS Sharpe / OOS Sharpe | < 3× | < 2× | < 1.5× |

---

## 6. Modèles de coût

### `BacktestConfig`

```python
@dataclass
class BacktestConfig:
    initial_capital:    float = 100_000.0
    commission_per_lot: float = 7.0      # USD par lot standard (round-trip)
    slippage_bps:       float = 1.0      # bps de l'ATR comme bruit gaussien
    spread_bps:         float = 2.0      # demi-spread ajouté au prix
    risk_free_rate:     float = 0.05     # taux sans risque annualisé (US T-bill)
    trading_days_per_year: int = 252
```

### Modèle de slippage

```
slippage ~ N(0, ATR × slippage_bps / 10000)

Sur EURUSD H1 :
  ATR moyen = 0.00080 (8 pips)
  slippage_bps = 1.0
  → slippage std = 0.00080 × 0.0001 = 0.000000080 ≈ 0.008 pip (très faible)
```

Le modèle gaussien est conservateur. En production, le slippage réel suit une distribution à queue épaisse (rare mais gros).

---

## 7. Pièges courants (biais à éviter)

### Look-ahead bias ⚠️

```python
# MAUVAIS : utilise close de la barre du signal pour remplir
fill_price = signal_bar.close   # on "connaît" ce prix avant la fin de la barre

# CORRECT : fill à l'open de la barre suivante
fill_price = next_bar.open
```

### Survivorship bias

Le backtest ne doit tester que des instruments qui existaient à la date historique. Éviter de tester sur `SPY` ou `AAPL` en 2000 si l'univers actuel est utilisé pour sélectionner les instruments.

### Data snooping

Tester des dizaines de combinaisons de paramètres jusqu'à trouver ce qui fonctionne. Solution : WFO + ajustement pour tests multiples (Bonferroni ou Benferroni-Holm).

### Biais de reconstruction

Utiliser des données révisées ou re-ajustées pour les splits/dividendes sur des données qui n'étaient pas disponibles à l'époque. Utiliser des données **point-in-time**.

### Over-fitting

```
Signal : Sharpe IS = 4.2, Sharpe OOS = 0.3 → over-fitté
Signal : Sharpe IS = 1.8, Sharpe OOS = 1.2 → robuste
```

Règle pratique : si OOS Sharpe < IS Sharpe / 3, la stratégie est probablement over-fittée.

---

## 8. Utilisation via l'API

### Via FastAPI REST

```bash
# Backtest simple
curl -X POST http://localhost:8000/api/v1/backtest/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol":          "EURUSD",
    "timeframe":       "H1",
    "strategy_id":     "ema_crossover_v1",
    "lookback_bars":   2000,
    "initial_capital": 100000.0,
    "strategy_config": {"fast_period": 9, "slow_period": 21}
  }'

# Walk-forward
curl -X POST "http://localhost:8000/api/v1/backtest/walk-forward?n_splits=5&in_sample_bars=1000&out_of_sample_bars=250" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"symbol": "EURUSD", "timeframe": "H1", "strategy_id": "ema_crossover_v1"}'
```

### Via Python direct

```python
from backtest_engine.service import BacktestEngine, BacktestConfig
from strategy_engine.service import EMACrossoverStrategy
from market_data.service import HistoricalDataManager

# 1. Charger les données
mgr  = HistoricalDataManager(mt5_provider=mt5, binance_provider=binance)
bars = await mgr.get_bars("EURUSD", "H1", lookback_bars=2000)

# 2. Configurer et lancer
config   = BacktestConfig(initial_capital=100_000.0, commission_per_lot=7.0)
engine   = BacktestEngine(config)
strategy = EMACrossoverStrategy(config={"fast_period": 9, "slow_period": 21})

result = await engine.run(strategy, bars, "EURUSD", "H1")

# 3. Accéder aux résultats
stats        = result["stats"]
equity_curve = result["equity_curve"]   # pd.Series
fills        = result["fills"]          # list[FillEvent]
bar_log      = result["bar_log"]        # list[dict]

print(f"Sharpe:      {stats['sharpe_ratio']:.3f}")
print(f"Max DD:      {stats['max_drawdown_pct']:.2f}%")
print(f"Win rate:    {stats['win_rate_pct']:.1f}%")
print(f"Profit F:   {stats['profit_factor']:.2f}")
print(f"Total trades:{stats['total_trades']}")
```

---

## 9. Interprétation des résultats

### Score de qualité d'une stratégie

```
Score = f(Sharpe, Profit Factor, Win Rate, Consistency WFO)

Seuils minimaux pour mise en production :
  Sharpe OOS              > 1.0
  Profit Factor           > 1.3
  Max Drawdown            < 20%
  Consistency WFO         > 60%
  Sharpe IS / Sharpe OOS  < 2.5×
  Total trades (stat sig) > 100
```

### Red flags

| Signal | Explication |
|---|---|
| Sharpe IS >> Sharpe OOS | Over-fitting |
| Profit Factor > 5.0 | Suspicieux — vérifier le biais |
| Win rate > 85% | Souvent dû à un look-ahead bias |
| Tous les trades profits les vendredis | Artefact de données |
| Max DD < 1% | Modèle de coûts insuffisant |

---

## 10. Limites et considérations

### Ce que le backtest ne peut pas modéliser

| Phénomène | Impact | Atténuation |
|---|---|---|
| Slippage de marché non-linéaire | Sous-estimation des coûts | Modèle d'impact de marché (Almgren-Chriss) |
| Liquidité intraday | Surestimation fill rate | Filtre sur volume/ADV |
| Margin calls | Ignore la contrainte de marge | Simuler le margin requirement |
| Changements de régime | Backtests bons en trend, nuls en range | Walk-forward + filtres de régime |
| Événements macro | Gaps non modélisés | Intégrer calendrier économique |
| Frais de financement overnight (swaps) | P&L surestimé sur positions multi-jours | Ajouter le coût de swap |

### Utiliser le backtest correctement

1. **Hypothesis first** : formuler l'hypothèse avant de tester
2. **Single test** : ne pas tester 50 variantes sur les mêmes données
3. **Paper trade first** : WFO OOS positif → paper trade 3 mois avant live
4. **Position sizing conservateur** : démarrer live avec 25% de la taille backtest
5. **Monitoring post-live** : comparer Sharpe live vs backtest OOS en continu

---

*Document précédent → [05_EXECUTION_ENGINE.md](05_EXECUTION_ENGINE.md)*  
*Document suivant → [07_MACHINE_LEARNING.md](07_MACHINE_LEARNING.md)*
