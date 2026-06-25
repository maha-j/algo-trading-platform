# 05 — Execution Engine

> **Niveau** : HFT Engineers, Senior Engineers  
> **Fichiers** : `execution_engine/service.py`  
> **Criticité** : ⚠️ Toute erreur ici entraîne des pertes financières réelles

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Machine à états des ordres](#2-machine-à-états-des-ordres)
3. [Algorithmes d'exécution](#3-algorithmes-dexécution)
4. [MT5BrokerAdapter](#4-mt5brokeradapter)
5. [Idempotency — protection anti-doublons](#5-idempotency--protection-anti-doublons)
6. [Slippage et attribution des coûts](#6-slippage-et-attribution-des-coûts)
7. [Cycle de vie complet d'un ordre](#7-cycle-de-vie-complet-dun-ordre)
8. [Gestion des erreurs et retry](#8-gestion-des-erreurs-et-retry)
9. [FillEvent et portfolio update](#9-fillevent-et-portfolio-update)
10. [Considérations de production](#10-considérations-de-production)

---

## 1. Vue d'ensemble

L'Execution Engine est la **couche de traduction** entre les ordres logiques (OrdreEvent) et le broker physique (MT5, Binance). C'est le composant avec les plus fortes exigences de fiabilité.

```
OrderEvent (risk_approved=True)
       │
       ▼
┌─────────────────────────────────────┐
│          EXECUTION ENGINE           │
│                                     │
│  ┌─────────────────────────────┐    │
│  │    Idempotency Check        │    │  ← Redis SETNX sur order_id
│  │    (duplicate prevention)   │    │
│  └─────────────┬───────────────┘    │
│                │                    │
│  ┌─────────────▼───────────────┐    │
│  │    Algorithm Selection      │    │
│  │  MARKET / TWAP / VWAP       │    │
│  └─────────────┬───────────────┘    │
│                │                    │
│  ┌─────────────▼───────────────┐    │
│  │    MT5BrokerAdapter         │    │  ← ThreadPoolExecutor
│  │    (sync C++ API isolated)  │    │
│  └─────────────┬───────────────┘    │
└───────────────┬┬────────────────────┘
                ││
     ┌──────────┘└──────────┐
     ▼                      ▼
FillEvent              OrderRecord
(publié sur          (persisté en DB)
stream:fills)
```

---

## 2. Machine à états des ordres

### États et transitions

```
                    submit_order()
                         │
                         ▼
                   PENDING_NEW
                    /         \
                  /             \
          broker accept      broker reject
                /                   \
               ▼                     ▼
          ACCEPTED               REJECTED  (terminal)
          /      \
        /          \
  partial fill    full fill
      /                \
     ▼                  ▼
PARTIALLY_FILLED     FILLED  (terminal)
      │
  cancel_order()
      │
      ▼
 PENDING_CANCEL
      │
      ▼
  CANCELLED  (terminal)
```

### Code de transition

```python
class OrderRecord:
    def transition(self, new_state: OrderState) -> None:
        """Transition atomique avec validation."""
        VALID_TRANSITIONS = {
            OrderState.PENDING_NEW:      [OrderState.ACCEPTED, OrderState.REJECTED],
            OrderState.ACCEPTED:         [OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                          OrderState.PENDING_CANCEL],
            OrderState.PARTIALLY_FILLED: [OrderState.FILLED, OrderState.PENDING_CANCEL],
            OrderState.PENDING_CANCEL:   [OrderState.CANCELLED],
        }
        allowed = VALID_TRANSITIONS.get(self.state, [])
        if new_state not in allowed:
            raise InvalidStateTransition(
                f"Cannot go from {self.state} → {new_state}"
            )
        self.state      = new_state
        self.updated_at = datetime.now(timezone.utc)
```

### `OrderRecord` — tracking du cycle de vie

```python
@dataclass
class OrderRecord:
    order_event:       OrderEvent      # événement original (immuable)
    state:             OrderState      # état courant
    broker_order_id:   str             # ID broker
    quantity_filled:   Decimal         # quantité exécutée
    avg_fill_price:    Decimal         # VWAP des fills
    commission:        Decimal         # commission totale
    created_at:        datetime
    updated_at:        datetime
    fills:             list[FillEvent] # tous les fills partiels
```

---

## 3. Algorithmes d'exécution

Tous les algorithmes implémentent `IExecutionAlgorithm` :

```python
class IExecutionAlgorithm(ABC):
    @property
    def name(self) -> str: ...

    @abstractmethod
    async def execute(
        self,
        order: OrderEvent,
        broker: MT5BrokerAdapter,
    ) -> list[FillEvent]:
        """Exécute l'ordre et retourne la liste des fills."""
        ...
```

### 3.1 MARKET — Exécution immédiate

Le plus simple : soumission immédiate au prix marché.

```
Signal
  │
  └──→ broker.submit_market_order(symbol, side, qty)
              │
              └──→ FillEvent (1 fill)
```

**Quand l'utiliser :**
- Signaux haute conviction nécessitant une exécution immédiate
- Ordres de clôture d'urgence (signal FLAT, circuit breaker)
- Petites tailles (< 0.1% de l'ADV)

```python
class MarketOrderAlgorithm(IExecutionAlgorithm):
    @property
    def name(self) -> str:
        return "MARKET"

    async def execute(self, order, broker) -> list[FillEvent]:
        fill = await broker.submit_market_order(
            symbol   = order.symbol,
            side     = order.side,
            quantity = order.quantity,
        )
        return [fill] if fill else []
```

---

### 3.2 TWAP — Time Weighted Average Price

Décompose un grand ordre en N tranches égales sur une durée configurée.

```
Ordre total : BUY 100,000 EURUSD

TWAP(slices=10, duration=300s):
  T+0s   → BUY 10,000  @ 1.08512
  T+30s  → BUY 10,000  @ 1.08515
  T+60s  → BUY 10,000  @ 1.08509
  ...
  T+270s → BUY 10,000  @ 1.08520
                              ↑
              Jitter ±20% sur le timing et la taille
              pour éviter la prédictibilité
```

**Jitter** : indispensable pour éviter qu'un prédateur de marché détecte le pattern régulier.

```python
class TWAPAlgorithm(IExecutionAlgorithm):
    async def execute(self, order, broker) -> list[FillEvent]:
        total_qty  = order.quantity
        n_slices   = self._config.get("slices", 10)
        duration   = self._config.get("duration_seconds", 300)
        interval   = duration / n_slices

        slice_qty  = total_qty / n_slices
        fills      = []
        remaining  = total_qty

        for i in range(n_slices):
            if remaining <= 0:
                break

            # Jitter ±20% sur la quantité
            jitter   = random.uniform(0.8, 1.2)
            qty      = min(slice_qty * jitter, remaining)

            fill = await broker.submit_market_order(
                order.symbol, order.side, qty
            )
            if fill:
                fills.append(fill)
                remaining -= qty

            # Jitter ±20% sur le délai
            sleep_time = interval * random.uniform(0.8, 1.2)
            await asyncio.sleep(sleep_time)

        return fills
```

---

### 3.3 VWAP — Volume Weighted Average Price

Adapte la cadence d'exécution au profil de volume intraday.

```
Volume intraday typique (Forex) :
    09h  ████████           (London open)
    10h  ██████████████     (pic)
    11h  ████████████
    12h  ██████             (midi)
    13h  ████████████████   (NY overlap)
    14h  ████████████████████ (pic NY)
    15h  ████████████████
    16h  ████████
    17h  ███
```

Le VWAP exécute plus d'unités pendant les heures de fort volume, minimisant l'impact marché.

```python
class VWAPAlgorithm(IExecutionAlgorithm):
    # Profil de volume U-shape (approximation intraday)
    _HOURLY_VOLUME_PROFILE = {
        0: 0.02, 1: 0.01, 2: 0.01, 3: 0.02,
        4: 0.03, 5: 0.04, 6: 0.06, 7: 0.08,
        8: 0.09, 9: 0.11, 10: 0.12, 11: 0.10,  # London
        12: 0.07, 13: 0.08, 14: 0.09, 15: 0.11, # NY overlap
        16: 0.08, 17: 0.05, 18: 0.03, 19: 0.02,
        20: 0.02, 21: 0.02, 22: 0.02, 23: 0.02,
    }

    async def execute(self, order, broker) -> list[FillEvent]:
        now          = datetime.now(timezone.utc)
        current_hour = now.hour

        # Pourcentage du total à exécuter maintenant
        volume_weight = self._HOURLY_VOLUME_PROFILE[current_hour]
        qty_now       = order.quantity * volume_weight

        if qty_now < 0.001:  # trop faible → passer à MARKET
            return await MarketOrderAlgorithm().execute(order, broker)

        fill = await broker.submit_market_order(order.symbol, order.side, qty_now)
        return [fill] if fill else []
```

### Sélection de l'algorithme

```python
def _select_algorithm(self, order: OrderEvent) -> IExecutionAlgorithm:
    algo_map = {
        "MARKET": MarketOrderAlgorithm(),
        "TWAP":   TWAPAlgorithm(config=self._settings.twap),
        "VWAP":   VWAPAlgorithm(config=self._settings.vwap),
    }
    return algo_map.get(order.algorithm, MarketOrderAlgorithm())
```

---

## 4. MT5BrokerAdapter

Le seul composant qui parle directement au broker. Isolé dans un `ThreadPoolExecutor`.

### Soumission d'un ordre marché

```python
async def submit_market_order(
    self,
    symbol:   str,
    side:     str,
    quantity: float,
) -> Optional[FillEvent]:

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        self._executor,
        self._send_order_sync,
        symbol, side, quantity,
    )
    return result
```

### `_send_order_sync` — exécuté dans le thread MT5

```python
def _send_order_sync(self, symbol, side, quantity) -> Optional[FillEvent]:
    import MetaTrader5 as mt5

    action_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    tick        = mt5.symbol_info_tick(symbol)
    price       = tick.ask if side == "BUY" else tick.bid

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    quantity,
        "type":      action_type,
        "price":     price,
        "deviation": 20,            # slippage max autorisé (points)
        "magic":     self._magic,   # identifiant de la stratégie
        "comment":   "trading_platform",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        return FillEvent(
            source     = "mt5",
            order_id   = str(result.order),
            symbol     = symbol,
            side       = side,
            quantity   = result.volume,
            fill_price = result.price,
            commission = result.comment_price if hasattr(result, "comment_price") else 0.0,
            timestamp  = datetime.now(timezone.utc),
        )
    else:
        logger.error("MT5 order failed: retcode=%d %s", result.retcode, result.comment)
        return None
```

### Reconnexion avec backoff

```python
async def connect(self) -> bool:
    for attempt in range(5):
        success = await loop.run_in_executor(self._executor, self._mt5_init)
        if success:
            return True
        wait = 2 ** attempt   # 1s, 2s, 4s, 8s, 16s
        logger.warning("MT5 reconnect attempt %d, waiting %ds", attempt + 1, wait)
        await asyncio.sleep(wait)
    return False
```

---

## 5. Idempotency — protection anti-doublons

Problème : en cas de timeout réseau, une requête peut être envoyée deux fois, créant un doublon d'ordre.

Solution : **vérification SETNX** sur `order_id` avant tout envoi.

```python
async def submit_order(self, order: OrderEvent) -> list[FillEvent]:
    # Guard : refus si order_id déjà soumis
    if order.order_id in self._submitted_ids:
        logger.warning(
            "Duplicate order_id=%s rejected (idempotency guard)",
            order.order_id
        )
        return []

    # Marquer comme soumis AVANT l'envoi broker
    # → même si le broker plante après, l'ordre est marqué
    self._submitted_ids.add(order.order_id)

    # Vérification risk_approved obligatoire
    if not order.risk_approved:
        logger.error("Order without risk approval rejected: %s", order.order_id)
        return []

    algo  = self._select_algorithm(order)
    fills = await algo.execute(order, self._broker)
    ...
```

> **Note production** : `_submitted_ids` est un `set` en mémoire. Pour la HA (redémarrage du service), persister dans Redis avec un TTL de 24h.

---

## 6. Slippage et attribution des coûts

Chaque `FillEvent` enregistre le slippage pour l'analyse post-trade.

```python
fill = FillEvent(
    fill_price = actual_price,       # prix réel obtenu
    slippage   = actual_price - expected_price,   # différence vs signal_price
    commission = commission_paid,
)
```

### Métriques Prometheus

```python
# Slippage en basis points
slippage_bps = abs(fill.slippage / fill.fill_price * 10000)
metrics.slippage_bps.labels(symbol=fill.symbol).observe(slippage_bps)

# Round-trip latency : signal_timestamp → fill_timestamp
round_trip = (fill.timestamp - order.signal_timestamp).total_seconds()
metrics.order_round_trip_seconds.labels(
    symbol=fill.symbol,
    algorithm=order.algorithm,
).observe(round_trip)
```

---

## 7. Cycle de vie complet d'un ordre

```
t=0ms    SignalEvent.direction = "LONG"  publié sur stream:signals
         │
t=0.5ms  container._wire: SignalEvent → portfolio.calculate_position_size()
         │  → qty = 10,000 unités (Kelly × Vol-Target)
         │
t=1ms    OrderEvent créé (risk_approved=True, algorithm="TWAP")
         │
t=1.2ms  execution_engine.submit_order(order)
         │  → idempotency check OK
         │  → TWAPAlgorithm.execute()
         │
t=2ms    Tranche 1 : broker.submit_market_order("EURUSD", "BUY", 1000)
         │  → ThreadPoolExecutor → mt5.order_send()
         │
t=18ms   MT5 retcode=DONE, fill_price=1.08514
         │
t=18.5ms FillEvent publié sur stream:fills
         │
t=19ms   portfolio_engine.on_fill(fill)
         │  → position EURUSD : net_qty = +1000, avg_entry = 1.08514
         │
t=30s    Tranche 2 (TWAP interval)...
         ...
t=5min   10ème tranche → ordre complet
```

---

## 8. Gestion des erreurs et retry

### Erreurs MT5 traitées

| Code retcode MT5 | Signification | Stratégie |
|---|---|---|
| `TRADE_RETCODE_DONE` (10009) | Succès | Créer FillEvent |
| `TRADE_RETCODE_REQUOTE` (10004) | Requote prix | Retry avec nouveau prix |
| `TRADE_RETCODE_TIMEOUT` (10010) | Timeout | Vérifier état puis retry ou cancel |
| `TRADE_RETCODE_INVALID_STOPS` (10016) | Stops invalides | Log + reject |
| `TRADE_RETCODE_NO_MONEY` (10019) | Marge insuffisante | RiskBreachEvent + alert |
| `TRADE_RETCODE_OFFQUOTES` (10006) | Hors cotation | Retry après 500ms |

### Retry avec backoff exponentiel

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(MT5ConnectionError),
)
async def _submit_with_retry(self, symbol, side, qty):
    return await self.broker.submit_market_order(symbol, side, qty)
```

---

## 9. FillEvent et portfolio update

Une fois le fill reçu :

```python
# Dans container._wire_event_subscriptions()
async def on_fill_dict(data: dict) -> None:
    fill = FillEvent(
        symbol     = data["symbol"],
        side       = data["side"],
        quantity   = float(data["quantity"]),
        fill_price = float(data["fill_price"]),
        commission = float(data["commission"]),
    )
    # 1. Mise à jour du portfolio (P&L, positions)
    await portfolio_engine.on_fill(fill)
    # 2. Persistance en DB (asyncpg)
    await fill_repository.save(fill)
    # 3. Notification si seuil configuré
    await notification_service.alert(
        subject=f"Fill: {fill.side} {fill.symbol}",
        body=f"Qty: {fill.quantity} @ {fill.fill_price}",
        level=AlertLevel.INFO,
    )
```

---

## 10. Considérations de production

### Ségrégation broker multi-compte

Pour une gestion multi-compte ou multi-stratégie :

```python
class OrderRouter:
    """Route les ordres vers le bon compte broker selon la stratégie."""
    def route(self, order: OrderEvent) -> MT5BrokerAdapter:
        account_map = {
            "ema_crossover_v1":   self._account_fx,
            "rsi_mean_reversion": self._account_eq,
        }
        return account_map.get(order.strategy_id, self._account_default)
```

### Pre-trade checks supplémentaires

```python
# À ajouter dans submit_order() avant l'envoi broker
async def _pre_trade_checks(self, order: OrderEvent) -> bool:
    # 1. Heure de marché (ne pas trader le dimanche 22h-23h)
    if not self._is_market_hours(order.symbol):
        return False
    # 2. Spread trop large (spread > 3× moyenne)
    if await self._spread_too_wide(order.symbol):
        return False
    # 3. Liquidité insuffisante (volume < seuil)
    if await self._insufficient_liquidity(order.symbol, order.quantity):
        return False
    return True
```

### Post-trade reconciliation

```python
# Tâche planifiée toutes les heures
async def reconcile_positions():
    """Compare les positions locales avec le broker."""
    local_positions  = portfolio_engine.get_positions()
    broker_positions = await broker.get_open_positions()

    for symbol, local_pos in local_positions.items():
        broker_pos = broker_positions.get(symbol)
        if broker_pos is None:
            logger.critical("RECONCILIATION: position %s in DB but not in broker!", symbol)
            await notification_service.alert("Reconciliation Error", ..., AlertLevel.CRITICAL)
```

---

*Document précédent → [04_RISK_ENGINE.md](04_RISK_ENGINE.md)*  
*Document suivant → [06_BACKTEST_ENGINE.md](06_BACKTEST_ENGINE.md)*
