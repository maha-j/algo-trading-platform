# 04 — Risk Engine

> **Niveau** : Risk Managers, Senior Quant Engineers  
> **Fichiers** : `risk_engine/service.py`, `portfolio_engine/service.py` (sizing)  
> **Criticité** : ⚠️ Composant le plus critique de la plateforme — aucune modification sans peer review

---

## Table des matières

1. [Rôle et responsabilités](#1-rôle-et-responsabilités)
2. [Chain of Responsibility](#2-chain-of-responsibility)
3. [Les 5 validateurs](#3-les-5-validateurs)
4. [VaR et CVaR — Historical Simulation](#4-var-et-cvar--historical-simulation)
5. [Circuit Breaker](#5-circuit-breaker)
6. [Position Sizing — Kelly × Vol-Targeting](#6-position-sizing--kelly--vol-targeting)
7. [Limites et paramètres](#7-limites-et-paramètres)
8. [RiskBreachEvent](#8-riskbreachevent)
9. [Audit et traçabilité](#9-audit-et-traçabilité)
10. [Considérations de production](#10-considérations-de-production)

---

## 1. Rôle et responsabilités

Le Risk Engine est le **gardien obligatoire** entre la logique de signal et l'exécution. Aucun ordre ne peut être soumis au broker sans passer par lui.

```
SignalEvent
    │
    ▼
╔═══════════════════════════════╗
║       RISK ENGINE             ║  ← Seul composant avec veto absolu
║                               ║
║  [1] Circuit breaker check    ║  ← Bloque tout si ouvert
║  [2] Chain of validators      ║  ← 5 validateurs séquentiels
║  [3] Approuvé ?               ║
║       ├─ OUI → OrderEvent     ║
║       └─ NON → RiskBreachEvent║
╚═══════════════════════════════╝
```

**Ce que le Risk Engine fait :**
- Valide chaque signal avant conversion en ordre
- Calcule VaR / CVaR en arrière-plan (ThreadPoolExecutor)
- Déclenche le circuit breaker en cas de breach critique
- Publie des `RiskBreachEvent` pour les notifications et l'audit

**Ce que le Risk Engine ne fait PAS :**
- Il ne modifie pas les positions (responsabilité du Portfolio Engine)
- Il ne calcule pas la taille des ordres (responsabilité du Portfolio Engine)
- Il ne connaît pas le broker (aucun import d'execution_engine)

---

## 2. Chain of Responsibility

Pattern GoF appliqué aux validateurs de risque. Chaque validateur est **indépendant, testable isolément, et composable**.

```
Signal arrive
    │
    ▼
┌────────────────────────┐
│ PositionLimitValidator │─── ❌ REJET → ValidationResult(approved=False)
└───────────┬────────────┘
            │ ✅
            ▼
┌────────────────────────────┐
│ MaxOpenPositionsValidator  │─── ❌ REJET
└───────────┬────────────────┘
            │ ✅
            ▼
┌────────────────────────┐
│  DailyLossValidator    │─── ❌ REJET
└───────────┬────────────┘
            │ ✅
            ▼
┌────────────────────────┐
│    VaRValidator        │─── ❌ REJET (calcul async ThreadPoolExecutor)
└───────────┬────────────┘
            │ ✅
            ▼
┌────────────────────────┐
│  DrawdownValidator     │─── ❌ REJET + CircuitBreaker.trip()
└───────────┬────────────┘
            │ ✅
            ▼
      SIGNAL APPROUVÉ
```

### Fail-fast

```python
async def validate_signal(self, signal, portfolio, returns) -> ValidationResult:
    # Court-circuit si circuit breaker ouvert
    if self._circuit_breaker.is_open():
        return ValidationResult(
            approved=False,
            validator_name="CircuitBreaker",
            message=f"Circuit breaker OPEN: {self._circuit_breaker._reason}",
        )

    # Chaîne de validateurs — s'arrête au premier rejet
    for validator in self._validators:
        result = await validator.validate(signal, portfolio, returns)
        if not result.approved:
            await self._handle_breach(signal, result)
            return result

    return ValidationResult(approved=True, validator_name="RiskEngine", message="All checks passed")
```

### `ValidationResult` — objet immuable

```python
@dataclass(frozen=True)
class ValidationResult:
    approved:        bool
    validator_name:  str
    message:         str
    metric_value:    float = 0.0   # valeur actuelle mesurée
    limit_value:     float = 0.0   # limite configurée
```

---

## 3. Les 5 validateurs

### 3.1 PositionLimitValidator

**Vérification** : La valeur de marché actuelle de la position sur ce symbole ne dépasse pas `max_position_size_pct` de l'equity totale.

```
current_market_value[symbol] / total_equity  ≤  max_position_size_pct
```

```python
current_pct = abs(position["market_value"]) / equity
approved    = current_pct < settings.max_position_size_pct  # défaut 5%
```

**Contournement intentionnel** : Les signaux FLAT (fermeture) ne sont PAS bloqués même si la position est à la limite — on veut toujours pouvoir fermer.

---

### 3.2 MaxOpenPositionsValidator

**Vérification** : Le nombre de positions ouvertes ne dépasse pas `max_open_positions`.

```python
open_count = len([p for p in positions.values() if abs(p["quantity"]) > 0])
approved   = open_count < settings.max_open_positions  # défaut 10
```

**Contournement** : Les signaux FLAT passent toujours (pour les sorties).

---

### 3.3 DailyLossValidator

**Vérification** : La perte réalisée du jour ne dépasse pas `max_daily_loss_pct`.

```
daily_loss / equity  ≤  max_daily_loss_pct
```

```python
daily_loss_pct = min(0.0, realised_pnl / equity)   # negative si perte
limit          = -settings.max_daily_loss_pct       # ex: -0.03 = -3%
approved       = daily_loss_pct > limit
```

> **Note production** : Calculer le P&L intraday depuis la table `fills` de TimescaleDB filtrée sur la date courante, pas depuis `get_realised_pnl()` qui est cumulatif.

---

### 3.4 VaRValidator

**Vérification** : Le VaR à 99% du portefeuille ne dépasse pas `max_portfolio_var_pct`.

**Méthode** : Historical Simulation (non-paramétrique) :

```
1. Collecter les N derniers returns journaliers (N = 252 par défaut)
2. Trier par ordre croissant
3. VaR 99% = percentile 1% de cette distribution
```

```python
# Calcul CPU-bound → ThreadPoolExecutor pour ne pas bloquer asyncio
loop    = asyncio.get_event_loop()
var_pct = await loop.run_in_executor(
    self._executor,
    self._compute_historical_var,
    returns_history,
    0.99,           # confidence level
)

# _compute_historical_var (exécuté dans un thread)
def _compute_historical_var(returns, confidence):
    percentile = (1.0 - confidence) * 100     # = 1.0 pour 99%
    var        = abs(np.percentile(returns, percentile))
    return var
```

**Limitations de la Historical Simulation** :
- Ne capture pas les événements hors-échantillon (cygnes noirs)
- Dépend de la quantité d'historique disponible
- Peut sous-estimer le risque en période de faible volatilité (avant une crise)

**En production, compléter avec** :
- Stress testing sur scénarios historiques (2008, 2020)
- Monte Carlo VaR
- Expected Shortfall (CVaR) comme mesure principale

---

### 3.5 DrawdownValidator

**Vérification** : Le drawdown courant depuis le pic d'equity ne dépasse pas `max_drawdown_pct`.

```python
cum_returns  = (1 + returns_history).cumprod()
peak         = cum_returns.cummax()
drawdown     = (cum_returns - peak) / peak
current_dd   = abs(float(drawdown.iloc[-1]))

approved = current_dd < settings.max_drawdown_pct   # défaut 15%
```

Si cette validation échoue avec action = "FLATTEN", le circuit breaker se déclenche.

---

## 4. VaR et CVaR — Historical Simulation

### Value at Risk (VaR)

Définition : perte maximale avec une probabilité de `(1-α)` sur un horizon de 1 jour.

```
VaR₉₉ = -percentile(returns, 1%)
```

**Exemple** :
```
Returns des 252 derniers jours, triés :
[-5.2%, -4.1%, -3.8%, ..., -0.1%, +0.0%, +0.1%, ..., +4.7%]
                ↑ percentile 1%

VaR₉₉ = 3.8%  → "95% de probabilité de ne pas perdre plus de 3.8% demain"
```

### Conditional VaR (CVaR / Expected Shortfall)

CVaR est **toujours ≥ VaR** — il mesure la perte espérée dans le pire 1% des cas :

```
CVaR₉₉ = E[Loss | Loss > VaR₉₉]
        = mean(returns[returns < -VaR₉₉])
```

```python
async def calculate_cvar(self, returns: np.ndarray, confidence: float = 0.99) -> float:
    if len(returns) < 30:
        return 0.0
    var_threshold = np.percentile(returns, (1 - confidence) * 100)
    tail          = returns[returns <= var_threshold]
    return float(abs(np.mean(tail))) if len(tail) > 0 else 0.0
```

### Comparaison VaR vs CVaR

| Métrique | Valeur typique | Interprétation |
|---|---|---|
| VaR 99% | 2.5% | Perte max probable au niveau 99% |
| CVaR 99% | 3.8% | Perte espérée dans le pire 1% des jours |
| CVaR / VaR ratio | 1.5 | Distribution avec queues épaisses (normal) |

---

## 5. Circuit Breaker

Le circuit breaker est le **mécanisme d'urgence** de la plateforme. Une fois ouvert, il bloque **absolument tous** les nouveaux ordres, indépendamment des validateurs.

### États

```
             breach critique
    CLOSED ──────────────────▶ OPEN
      ▲                          │
      │       reset manuel       │
      └──────────────────────────┘
        (operator_id requis)
```

### Déclenchement automatique

```python
async def _handle_breach(self, signal: SignalEvent, result: ValidationResult) -> None:
    # Publier l'event de breach
    breach = RiskBreachEvent(
        source="risk_engine",
        symbol=signal.symbol,
        breach_type=result.validator_name,
        current_value=result.metric_value,
        limit_value=result.limit_value,
        action="BLOCK" if result.metric_value < result.limit_value * 1.5 else "FLATTEN",
    )
    await self._event_bus.publish(breach)

    # Ouvrir le circuit breaker si action critique
    if breach.action == "FLATTEN":
        self._circuit_breaker.trip(reason=result.message)
        logger.critical("CIRCUIT BREAKER OPENED: %s", result.message)
```

### Reset manuel (admin uniquement)

```python
# Via l'API REST — requiert role="admin" dans le JWT
POST /api/v1/risk/circuit-breaker/reset

# Appel interne
engine._circuit_breaker.reset(operator_id="admin_user_id")
```

```python
class CircuitBreaker:
    def trip(self, reason: str) -> None:
        self._is_open = True
        self._reason  = reason
        self._tripped_at = datetime.now(timezone.utc)
        logger.critical("CIRCUIT BREAKER TRIPPED: %s", reason)

    def reset(self, operator_id: str) -> None:
        self._is_open = False
        logger.warning("Circuit breaker reset by operator: %s", operator_id)

    def is_open(self) -> bool:
        return self._is_open
```

---

## 6. Position Sizing — Kelly × Vol-Targeting

Le sizing est géré dans `portfolio_engine/service.py` par la classe `PositionSizer`.

### Méthode 1 : Volatility Targeting

```
size = (equity × target_vol_pct) / (daily_vol_asset × price)
```

Exemple :
```
equity          = 100,000 USD
target_vol_pct  = 1% par position
daily_vol_EURUSD = 0.6% (ATR/price)
price           = 1.0850

vol_target_size = (100000 × 0.01) / (0.006 × 1.0850)
               = 1000 / 0.00651
               = ~153,609 unités → ~1.53 lots standard
```

### Méthode 2 : Kelly Criterion (half-Kelly)

```
f* = W - (1-W)/R    où W = win_rate, R = avg_win/avg_loss

half_Kelly = f* × 0.5   (cap standard en hedge fund)
size = equity × half_Kelly / price
```

Exemple :
```
win_rate = 55%, avg_win = 1.5%, avg_loss = 1.0%
R        = 1.5
f*       = 0.55 - 0.45/1.5 = 0.55 - 0.30 = 0.25 (25%)
half_K   = 0.25 × 0.5 = 12.5%

kelly_size = 100,000 × 0.125 / 1.085 = ~11,521 unités
```

### Blend final : min(vol_target, half_kelly)

```python
def recommended_size(self, equity, price, daily_vol, win_rate, avg_win, avg_loss) -> Decimal:
    vs = self.vol_target_size(equity, price, daily_vol)
    ks = self.kelly_size(equity, price, win_rate, avg_win, avg_loss)
    if ks == 0:
        return vs
    return min(vs, ks)   # approche conservative : prendre le plus petit
```

Le résultat est ensuite cappé par `max_position_pct` (5% de l'equity par défaut).

---

## 7. Limites et paramètres

Tous les paramètres sont définis dans `config/settings.py` via des variables d'environnement.

| Paramètre | Variable env | Défaut | Description |
|---|---|---|---|
| `max_position_size_pct` | `RISK_MAX_POSITION_SIZE_PCT` | 5% | Position max par symbole / equity |
| `max_open_positions` | `RISK_MAX_OPEN_POSITIONS` | 10 | Nombre max de positions simultanées |
| `max_daily_loss_pct` | `RISK_MAX_DAILY_LOSS_PCT` | 3% | Perte journalière max |
| `max_drawdown_pct` | `RISK_MAX_DRAWDOWN_PCT` | 15% | Drawdown max depuis peak equity |
| `max_portfolio_var_pct` | `RISK_MAX_VAR_PCT` | 2% | VaR 99% max du portefeuille |
| `var_confidence_level` | `RISK_VAR_CONFIDENCE` | 99% | Niveau de confiance VaR |
| `var_lookback_days` | `RISK_VAR_LOOKBACK` | 252 | Fenêtre de données pour VaR |

### Configuration via `.env`

```bash
RISK_MAX_POSITION_SIZE_PCT=5.0
RISK_MAX_DAILY_LOSS_PCT=3.0
RISK_MAX_DRAWDOWN_PCT=15.0
RISK_MAX_OPEN_POSITIONS=10
```

---

## 8. RiskBreachEvent

Événement publié sur `stream:risk` à chaque validation échouée.

```python
@dataclass(frozen=True, slots=True)
class RiskBreachEvent(BaseEvent):
    symbol:        str
    breach_type:   str     # "DrawdownValidator", "VaRValidator"...
    current_value: float   # valeur mesurée
    limit_value:   float   # limite configurée
    action:        str     # "BLOCK" | "FLATTEN" | "HALT"

    @property
    def is_critical(self) -> bool:
        return self.action in ("FLATTEN", "HALT")
```

### Consommateurs de `stream:risk`

| Consommateur | Action sur breach |
|---|---|
| `NotificationService` | Telegram CRITICAL + Email + Slack |
| `Dashboard` | Alerte rouge sur le Risk Monitor |
| `AuditLogRepository` | Insertion immuable dans `risk_events` |
| `ExecutionEngine` (si FLATTEN) | Fermeture de toutes les positions |

---

## 9. Audit et traçabilité

Chaque validation est loguée selon son résultat :

```python
# Approbation (niveau DEBUG)
logger.debug(
    "Risk approved | %s | %s | %s",
    signal.strategy_id, signal.symbol, result.message
)

# Rejet (niveau WARNING)
logger.warning(
    "Risk REJECTED | %s | %s | validator=%s | metric=%.4f | limit=%.4f",
    signal.symbol, signal.direction,
    result.validator_name, result.metric_value, result.limit_value,
)

# Breach critique (niveau CRITICAL)
logger.critical(
    "RISK BREACH | type=%s | value=%.4f | limit=%.4f | action=%s",
    breach.breach_type, breach.current_value, breach.limit_value, breach.action,
)
```

Les breach critiques sont également persistés dans la table `risk_events` :

```sql
INSERT INTO risk_events (time, breach_type, current_value, limit_value, action)
VALUES (NOW(), $1, $2, $3, $4);
```

---

## 10. Considérations de production

### Ce qui doit être amélioré pour une vraie mise en prod

#### VaR multi-actifs avec corrélation

La version actuelle calcule le VaR indépendamment. En production :

```python
# Matrice de corrélation cross-actifs
correlation_matrix = returns_df.corr()
portfolio_weights  = np.array([pos["weight"] for pos in positions])
portfolio_vol      = np.sqrt(
    portfolio_weights @ (cov_matrix @ portfolio_weights)
)
var_parametric = norm.ppf(0.99) * portfolio_vol * equity
```

#### Stress testing

```python
# Scénarios historiques
STRESS_SCENARIOS = {
    "2008_crisis":     {"equity_shock": -0.40, "vol_spike": 3.0},
    "covid_march_2020": {"equity_shock": -0.35, "vol_spike": 2.5},
    "flash_crash_2010": {"equity_shock": -0.10, "vol_spike": 5.0},
}
```

#### Limites intraday vs EOD

En production, distinguer :
- **Limites intraday** : plus strictes, reset à la clôture
- **Limites EOD** : pour le reporting de risque de fin de journée

#### Séparation des responsabilités

Le Risk Engine ne devrait pas avoir accès direct au Portfolio Engine. En production, passer par un `RiskDataSnapshot` immutable :

```python
@dataclass(frozen=True)
class RiskSnapshot:
    equity:            Decimal
    open_positions:    int
    daily_pnl:         Decimal
    returns_30d:       np.ndarray
    current_drawdown:  float
```

#### Rate limiting des calculs VaR

Le calcul VaR est coûteux. Éviter de le recalculer à chaque signal :

```python
# Cache Redis avec TTL 30 secondes
@cached(cache=Redis(ttl=30), key="var:portfolio")
async def calculate_var(self) -> float: ...
```

---

*Document précédent → [03_STRATEGY_ENGINE.md](03_STRATEGY_ENGINE.md)*  
*Document suivant → [05_EXECUTION_ENGINE.md](05_EXECUTION_ENGINE.md)*
