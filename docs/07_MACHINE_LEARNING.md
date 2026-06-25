# 07 — Machine Learning Engine

> **Niveau** : ML Engineers, Quant Researchers  
> **Fichiers** : `ml_engine/service.py`

---

## Table des matières

1. [Vue d'ensemble et philosophie](#1-vue-densemble-et-philosophie)
2. [Feature Engineering](#2-feature-engineering)
3. [RegimeClassifier — GMM](#3-regimeclassifier--gmm)
4. [ReturnPredictor — LightGBM](#4-returnpredictor--lightgbm)
5. [AnomalyDetector — Isolation Forest](#5-anomalydetector--isolation-forest)
6. [MLflow — tracking et versioning](#6-mlflow--tracking-et-versioning)
7. [Pipeline d'entraînement offline](#7-pipeline-dentraînement-offline)
8. [Inférence en temps réel](#8-inférence-en-temps-réel)
9. [Intégration avec le Strategy Engine](#9-intégration-avec-le-strategy-engine)
10. [Considérations production](#10-considérations-production)

---

## 1. Vue d'ensemble et philosophie

### Rôle du ML dans la plateforme

Le ML n'est **pas** le générateur de signaux principal. Il joue un rôle de **filtre de confiance** et de **détection de régime** :

```
Signal brut (ema_crossover)
        │
        ▼
┌───────────────────────────────────────┐
│              ML LAYER                  │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │  RegimeClassifier                │  │
│  │  "Sommes-nous en tendance ?"     │  │
│  │  regime = "ranging" → pas de     │  │
│  │  signal EMA (stratégie inadaptée)│  │
│  └──────────────────────────────────┘  │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │  ReturnPredictor (LightGBM)      │  │
│  │  "Quelle proba de hausse ?"      │  │
│  │  prob < 0.55 → signal filtré     │  │
│  └──────────────────────────────────┘  │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │  AnomalyDetector                 │  │
│  │  "Micro-structure normale ?"     │  │
│  │  score < -0.5 → suspendre        │  │
│  └──────────────────────────────────┘  │
└───────────────────────────────────────┘
        │
        ▼
Signal filtré (ou bloqué)
```

### Séparation entraînement / inférence

```
OFFLINE (nightly batch)
  → Charger 2 ans de données OHLCV
  → FeatureEngine.build_features()
  → RegimeClassifier.fit()
  → ReturnPredictor.fit()
  → AnomalyDetector.fit()
  → Sauvegarder en pickle + MLflow registry

ONLINE (< 1ms par signal)
  → Charger modèles depuis /tmp/*.pkl
  → FeatureEngine.build_features(derniers 100 bars)
  → predict_proba() ou score()
```

---

## 2. Feature Engineering

### Principe de construction

Toutes les features doivent être :
- **Stationnaires** : pas de prix bruts, seulement des rendements, ratios, z-scores
- **Bornées** : winsorisées à ±5σ pour les outliers
- **Débiaisées** : aucune feature forward-looking

### Catégories de features (30 au total)

#### Momentum (returns multi-échelles)

```python
for lb in [1, 3, 5, 10, 20, 60]:
    feats[f"ret_{lb}"] = np.log(close / close.shift(lb))
```

#### Volatilité

```python
for lb in [5, 10, 20, 60]:
    ret = np.log(close / close.shift(1))
    feats[f"vol_{lb}"] = ret.rolling(lb).std() * np.sqrt(252)

# Estimateur Parkinson (plus efficace que close-to-close)
feats["parkinson_vol"] = np.sqrt(
    1 / (4 * np.log(2)) * (np.log(high / low) ** 2).rolling(20).mean()
)

feats["atr_ratio"] = tr.rolling(14).mean() / close
```

#### Trend

```python
for fast, slow in [(9, 21), (21, 50), (50, 200)]:
    ema_fast = close.ewm(span=fast).mean()
    ema_slow = close.ewm(span=slow).mean()
    feats[f"ema_spread_{fast}_{slow}"] = (ema_fast - ema_slow) / ema_slow

# Pente linéaire normalisée (trend slope)
def rolling_slope(series, window):
    return series.rolling(window).apply(
        lambda y: np.polyfit(range(len(y)), y, 1)[0] / y.mean()
    )
feats["slope_20"] = rolling_slope(close, 20)

# Bollinger Band position
feats["bb_position"] = (close - sma20) / (2 * std20 + 1e-10)
feats["rsi_norm"]    = rsi / 50 - 1   # normalisé [-1, 1]
```

#### Volume

```python
feats["vol_zscore"] = (volume - vol_mean) / vol_std
feats["vol_trend"]  = np.log(volume.rolling(5).mean() / volume.rolling(20).mean())

# OBV momentum
obv = (np.sign(close.diff()) * volume).cumsum()
feats["obv_momentum"] = obv.pct_change(5)
```

#### Microstructure (candlestick patterns)

```python
bar_range = high - low
feats["upper_shadow"] = (high - close.clip(upper=high)) / (bar_range + 1e-10)
feats["lower_shadow"] = (close.clip(lower=low) - low)   / (bar_range + 1e-10)
feats["body_ratio"]   = abs(close - open) / (bar_range + 1e-10)
```

### Target variable

```python
def build_target(df, forward_bars=1) -> pd.Series:
    """
    1 si prix monte dans les N prochaines barres, 0 sinon.
    Shift de 1 pour éviter le look-ahead bias.
    """
    future_return = df["close"].shift(-forward_bars) / df["close"] - 1
    target = (future_return > 0).astype(int)
    return target.shift(1).fillna(0)   # lag obligatoire
```

---

## 3. RegimeClassifier — GMM

### Intuition

Les marchés financiers ne suivent pas un seul processus stochastique. Ils alternent entre des **régimes** distincts :

```
Régime 0 — trending_up    : EMA divergente, volume croissant, momentum fort
Régime 1 — trending_down  : Idem mais baissier
Régime 2 — ranging        : EMA convergente, faible volatilité, RSI ~50
Régime 3 — volatile       : Large dispersion, volumes atypiques, gaps
```

### Modèle : Gaussian Mixture Model (GMM)

La GMM modélise la distribution jointe de (returns, volatilité) comme un mélange de 4 gaussiennes.

```python
from sklearn.mixture import GaussianMixture

# Features 2D : [mean_return, volatility]
def fit(self, df):
    returns = np.log(df["close"] / df["close"].shift(1)).dropna()
    vol20   = returns.rolling(20).std().dropna()

    features = np.column_stack([returns.values, vol20.values])
    features_scaled = StandardScaler().fit_transform(features)

    self._model = GaussianMixture(
        n_components     = 4,
        covariance_type  = "full",
        max_iter         = 200,
        random_state     = 42,
    )
    self._model.fit(features_scaled)
```

### Inférence (< 0.5ms)

```python
def predict(self, df, window=20) -> int:
    recent     = df.iloc[-window:]
    mean_ret   = np.log(recent["close"] / recent["close"].shift(1)).mean()
    volatility = np.log(recent["close"] / recent["close"].shift(1)).std()

    features_scaled = self._scaler.transform([[mean_ret, volatility]])
    regime          = int(self._model.predict(features_scaled)[0])
    return regime

def get_regime_label(self, df) -> str:
    return {0: "trending_up", 1: "trending_down", 2: "ranging", 3: "volatile"}[
        self.predict(df)
    ]
```

### Utilisation dans la stratégie

```python
# Dans EMACrossoverStrategy.on_bar()
regime = ml_engine.get_regime("EURUSD", bars)
if regime in ("ranging", "volatile"):
    return None   # Ne pas trader un croisement EMA dans un marché sans tendance
```

---

## 4. ReturnPredictor — LightGBM

### Choix du modèle

| Modèle | Avantages | Inconvénients |
|---|---|---|
| LightGBM | Rapide, robuste, peu de tuning | Boîte noire, peut sur-fitter |
| RandomForest | Très robuste, interprétable | Plus lent, moins précis |
| Neural Network | Capture les non-linéarités complexes | Besoin de beaucoup de données, sur-fit |
| Logistic Regression | Interprétable, rapide | Linéaire — ne capture pas les interactions |

**Choix : LightGBM** (fallback RandomForest si non installé), standard en ML financier institutionnel.

### Entraînement

```python
def fit(self, df) -> "ReturnPredictor":
    X = FeatureEngine.build_features(df)
    y = FeatureEngine.build_target(df, forward_bars=1)

    # Split temporel strict — JAMAIS de shuffling sur des séries temporelles
    split   = int(len(X) * 0.8)
    X_train = X.iloc[:split]
    X_test  = X.iloc[split:]
    y_train = y.iloc[:split]
    y_test  = y.iloc[split:]

    self._pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("model", LGBMClassifier(
            n_estimators     = 300,
            learning_rate    = 0.05,
            num_leaves       = 31,
            min_child_samples = 20,
            colsample_bytree = 0.8,
            subsample        = 0.8,
            random_state     = 42,
            verbose          = -1,
        )),
    ])
    self._pipeline.fit(X_train, y_train)
```

### Validation temporelle

```python
# TimeSeriesSplit : les folds respectent l'ordre chronologique
from sklearn.model_selection import TimeSeriesSplit, cross_val_score

tscv   = TimeSeriesSplit(n_splits=5)
scores = cross_val_score(
    self._pipeline, X, y,
    cv=tscv, scoring="roc_auc"
)
print(f"CV AUC: {scores.mean():.3f} ± {scores.std():.3f}")
```

### Inférence

```python
def predict_proba(self, df, last_n=100) -> float:
    """Retourne P(price_up) pour la dernière barre. < 1ms."""
    X = FeatureEngine.build_features(df.iloc[-last_n:])
    if X.empty:
        return 0.5   # neutre si pas assez de données

    prob = float(self._pipeline.predict_proba(X.iloc[[-1]])[:, 1][0])
    return prob

def is_confident(self, df) -> tuple[bool, float]:
    """True si la probabilité dépasse le seuil de confiance."""
    prob = self.predict_proba(df)
    if prob >= self.confidence_threshold:
        return True, prob           # signal LONG
    if (1 - prob) >= self.confidence_threshold:
        return True, 1 - prob       # signal SHORT
    return False, prob              # pas de conviction
```

---

## 5. AnomalyDetector — Isolation Forest

### Intuition

L'Isolation Forest détecte les anomalies en isolant les points de données. Un point "normal" nécessite beaucoup de partitions pour être isolé ; un point anormal est isolé rapidement.

**Anomalies cibles** :
- Flash crash (prix spike + volume spike)
- Erreur de feed (prix = 0, volume = 0)
- Événement macro extrême créant un régime break

### Features utilisées

```python
features_cols = ["ret_1", "vol_5", "vol_zscore", "parkinson_vol", "bb_position"]
```

### Entraînement

```python
from sklearn.ensemble import IsolationForest

self._model = IsolationForest(
    contamination  = 0.01,    # 1% des points considérés comme anomalies
    n_estimators   = 100,
    random_state   = 42,
    n_jobs         = -1,
)
self._model.fit(X.values)
```

### Score d'anomalie

```python
def score(self, df) -> float:
    """Score dans [-inf, 0]. Plus négatif = plus anormal."""
    X     = FeatureEngine.build_features(df.iloc[-20:])[features_cols].dropna()
    score = float(self._model.score_samples(X.iloc[[-1]])[0])
    return score

def is_anomaly(self, df, threshold=-0.5) -> bool:
    return self.score(df) < threshold
```

### Intégration avec le Risk Engine

```python
# Dans risk_engine.validate_signal()
if ml_engine.is_anomaly(symbol, bars):
    return ValidationResult(
        approved       = False,
        validator_name = "AnomalyDetector",
        message        = f"Anomalous market microstructure detected for {symbol}",
    )
```

---

## 6. MLflow — tracking et versioning

### Métriques loguées à chaque entraînement

```python
with mlflow.start_run():
    mlflow.log_params({
        "symbol":        symbol,
        "training_rows": len(df),
        "forward_bars":  predictor.forward_bars,
        "model_type":    "LightGBM" if LGBM_AVAILABLE else "RandomForest",
        "n_features":    X.shape[1],
    })

    mlflow.log_metrics({
        "train_auc":     auc_train,
        "test_auc":      auc_test,
        "accuracy":      acc_test,
        "fi_ret_1":      feature_importance["ret_1"],
        "fi_vol_20":     feature_importance["vol_20"],
        "fi_rsi_norm":   feature_importance["rsi_norm"],
    })

    mlflow.sklearn.log_model(
        pipeline,
        "return_predictor",
        registered_model_name=f"ReturnPredictor_{symbol}",
    )
```

### Accès MLflow UI

```bash
# Depuis le navigateur
http://localhost:5000

# Depuis Python
client = mlflow.MlflowClient()
models = client.search_model_versions("name='ReturnPredictor_EURUSD'")
```

### Model Registry

```python
# Promouvoir en production
client.transition_model_version_stage(
    name    = "ReturnPredictor_EURUSD",
    version = "3",
    stage   = "Production",
)

# Charger le modèle en production
model = mlflow.sklearn.load_model("models:/ReturnPredictor_EURUSD/Production")
```

---

## 7. Pipeline d'entraînement offline

```python
# scripts/train_ml_models.py
# À exécuter quotidiennement (Celery beat ou cron)

async def train_all_models():
    symbols = ["EURUSD", "GBPUSD", "BTCUSDT", "ETHUSDT"]

    for symbol in symbols:
        logger.info("Training ML models for %s", symbol)

        # 1. Charger les données
        bars = await hist_manager.get_bars(
            symbol, "H1", lookback_bars=5000
        )
        if len(bars) < 500:
            logger.warning("Insufficient data for %s", symbol)
            continue

        # 2. Entraîner tous les modèles
        ml_engine.train(symbol, bars)

        # 3. Vérifier la qualité
        predictor = ml_engine._predictors[symbol]
        if predictor._pipeline:
            prob = predictor.predict_proba(bars)
            logger.info("Model ready: %s P(up)=%.3f", symbol, prob)
```

---

## 8. Inférence en temps réel

```python
class MLEngine:
    def get_signal_confidence(self, symbol: str, df: pd.DataFrame) -> float:
        """Retourne P(prix monte) ∈ [0, 1]. 0.5 = neutre."""
        if symbol not in self._predictors:
            return 0.5   # pas de modèle → neutre
        return self._predictors[symbol].predict_proba(df)

    def get_regime(self, symbol: str, df: pd.DataFrame) -> str:
        if symbol not in self._classifiers:
            return "unknown"
        return self._classifiers[symbol].get_regime_label(df)

    def is_anomaly(self, symbol: str, df: pd.DataFrame) -> bool:
        if symbol not in self._detectors:
            return False
        return self._detectors[symbol].is_anomaly(df)

    def is_trained(self, symbol: str) -> bool:
        return symbol in self._trained
```

### Latence d'inférence

| Modèle | Latence typique | Notes |
|---|---|---|
| `RegimeClassifier.predict()` | ~0.3 ms | 2 features scalées |
| `ReturnPredictor.predict_proba()` | ~0.8 ms | 30 features, LightGBM |
| `AnomalyDetector.score()` | ~0.5 ms | 5 features, Isolation Forest |
| **Total ML layer** | **~1.6 ms** | Acceptable dans le budget latence |

---

## 9. Intégration avec le Strategy Engine

### Pattern recommandé

```python
class EMACrossoverWithML(BaseStrategy):
    def __init__(self, config, ml_engine: MLEngine):
        super().__init__(config)
        self._ml = ml_engine

    async def on_bar(self, bar: BarEvent, ind: IndicatorService) -> SignalEvent | None:
        # Signal technique de base
        signal = await self._ema_crossover_signal(bar, ind)
        if signal is None:
            return None

        bars = ind.get_cached_df(bar.symbol, bar.timeframe)

        # Filtre 1 : régime de marché
        regime = self._ml.get_regime(bar.symbol, bars)
        if regime in ("ranging", "volatile") and abs(signal.strength) < 0.7:
            logger.debug("Signal filtered: wrong regime (%s)", regime)
            return None

        # Filtre 2 : anomalie de microstructure
        if self._ml.is_anomaly(bar.symbol, bars):
            logger.warning("Signal blocked: microstructure anomaly")
            return None

        # Filtre 3 : ajustement de la force par la confiance ML
        confidence = self._ml.get_signal_confidence(bar.symbol, bars)
        adjusted_strength = signal.strength * confidence

        if adjusted_strength < 0.3:
            return None   # confiance trop faible

        return SignalEvent(
            **signal.__dict__,
            strength=adjusted_strength,
        )
```

---

## 10. Considérations production

### Séparation stricte entraînement / inférence

```
❌ Mauvais : réentraîner le modèle sur chaque nouvelle barre (adaption = overfitting)

✅ Correct :
  - Entraînement : batch hebdomadaire ou mensuel
  - Inférence    : utilisation du modèle figé jusqu'au prochain entraînement
  - Monitoring   : détecter le concept drift → déclencher réentraînement anticipé
```

### Concept drift detection

```python
# Surveiller la distribution des prédictions
mean_prob = np.mean([ml_engine.get_signal_confidence(sym, df)])

# Si P(up) dérive vers ~0.4 ou ~0.6 en continu → concept drift
if abs(mean_prob - 0.5) > 0.15:
    logger.warning("Potential concept drift detected: mean_prob=%.3f", mean_prob)
    trigger_retraining()
```

### Ce qu'il faut ajouter en production

- **Feature store** (Redis/Feast) : partager les features précalculées entre les modèles
- **A/B testing** : comparer les performances de deux versions de modèle en production
- **Shadow mode** : faire tourner le nouveau modèle sans l'utiliser, mesurer les prédictions
- **Calibration** : s'assurer que les probabilités sont bien calibrées (Platt scaling)
- **Explainability** : SHAP values pour comprendre chaque prédiction
- **Model serving** : REST endpoint MLflow ou BentoML pour découpler l'inférence du trading

---

*Document précédent → [06_BACKTEST_ENGINE.md](06_BACKTEST_ENGINE.md)*  
*Document suivant → [08_MONITORING.md](08_MONITORING.md)*
