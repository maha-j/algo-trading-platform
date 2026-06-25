# 11 — Sécurité

> **Niveau** : Security Engineers, DevOps, Lead Engineers  
> **Fichiers** : `api/main.py`, `config/settings.py`, `docker/docker-compose.yml`  
> **Criticité** : ⚠️ Ce document couvre des exigences non négociables pour une mise en production

---

## Table des matières

1. [Modèle de menaces](#1-modèle-de-menaces)
2. [Authentification JWT](#2-authentification-jwt)
3. [TLS et transport security](#3-tls-et-transport-security)
4. [Gestion des secrets](#4-gestion-des-secrets)
5. [Rate limiting et protection DDoS](#5-rate-limiting-et-protection-ddos)
6. [Sécurité des credentials broker](#6-sécurité-des-credentials-broker)
7. [Audit trail](#7-audit-trail)
8. [Sécurité des containers](#8-sécurité-des-containers)
9. [Checklist OWASP Top 10](#9-checklist-owasp-top-10)
10. [Réponse aux incidents](#10-réponse-aux-incidents)

---

## 1. Modèle de menaces

### Actifs à protéger

| Actif | Impact en cas de compromission |
|---|---|
| Credentials broker MT5 | Accès à tous les fonds — critique |
| JWT secret | Usurpation d'identité, ordres non autorisés |
| Données de position | Information privilégiée, front-running |
| Base de données | Historique complet des stratégies |
| Clé API Telegram | Spam, leak des alertes de position |

### Vecteurs d'attaque prioritaires

```
1. Injection SQL dans les endpoints API → Parameterized queries (asyncpg)
2. JWT forgé → Signature HS256 avec secret fort (≥32 chars)
3. Replay d'ordre → Idempotency key (order_id UUID)
4. Credential leak → Variables d'env + Docker Secrets + rotation
5. DDoS API → Rate limiting + nginx throttling
6. Insider threat → Audit log immuable + alertes sur actions sensibles
```

---

## 2. Authentification JWT

### Middleware d'authentification

```python
# api/main.py — toutes les routes /api/v1/* requièrent un JWT valide
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

async def require_auth(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(
            token,
            settings.api.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
        )
        return payload   # {"sub": "user_id", "role": "trader", "exp": ...}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
```

### Génération de tokens

```python
# scripts/generate_token.py
from datetime import datetime, timedelta, timezone
from jose import jwt

def create_access_token(user_id: str, role: str, expiry_hours: int = 8):
    expire  = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    payload = {
        "sub":  user_id,
        "role": role,          # "admin" | "trader" | "viewer"
        "exp":  expire,
        "iat":  datetime.now(timezone.utc),
        "jti":  str(uuid4()),  # JWT ID — pour la revocation
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")
```

### RBAC — contrôle d'accès par rôle

```python
def require_role(required_role: str):
    async def checker(auth = Depends(require_auth)):
        user_role = auth.get("role")
        role_hierarchy = {"viewer": 0, "trader": 1, "admin": 2}
        if role_hierarchy.get(user_role, -1) < role_hierarchy.get(required_role, 99):
            raise HTTPException(status_code=403, detail=f"Role '{required_role}' required")
        return auth
    return checker

# Usage
@router.post("/circuit-breaker/reset")
async def reset_cb(auth = Depends(require_role("admin"))):
    ...
```

| Role | Permissions |
|---|---|
| `viewer` | GET uniquement (portfolio, positions, métriques) |
| `trader` | + soumettre des ordres manuels |
| `admin` | + reset circuit breaker, activer/désactiver stratégies |

### Secret JWT — exigences

```bash
# ✅ Minimum 32 caractères, générés aléatoirement
openssl rand -hex 32
# → a7f3d2e1b4c6f8a2d3e4f5a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1

# ❌ JAMAIS :
JWT_SECRET=mysecret
JWT_SECRET=trading123
JWT_SECRET=changeme
```

---

## 3. TLS et transport security

### nginx — configuration TLS

```nginx
# docker/nginx.conf

server {
    listen 443 ssl http2;
    server_name trading.yourfirm.com;

    # TLS 1.3 uniquement (1.2 toléré pour compatibilité legacy)
    ssl_protocols             TLSv1.2 TLSv1.3;
    ssl_ciphers               ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;
    ssl_session_cache         shared:SSL:10m;
    ssl_session_timeout       1d;
    ssl_session_tickets       off;

    # HSTS — forcer HTTPS pour 1 an
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Certificat (Let's Encrypt ou propre)
    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;

    location / {
        proxy_pass http://fastapi:8000;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Redirection HTTP → HTTPS
server {
    listen 80;
    return 301 https://$host$request_uri;
}
```

### Redis TLS

```yaml
# docker-compose.yml
redis:
  command: >
    redis-server
    --tls-port 6380
    --tls-cert-file /tls/redis.crt
    --tls-key-file  /tls/redis.key
    --tls-ca-cert-file /tls/ca.crt
```

### PostgreSQL SSL

```bash
# settings.py
DATABASE_URL = "postgresql://trading:pass@localhost/trading?sslmode=require"
```

---

## 4. Gestion des secrets

### Principe : zéro secret dans le code

```python
# ✅ Correct — SecretStr masque la valeur dans les logs
class MT5Settings(BaseSettings):
    password: SecretStr

# Dans les logs :
# MT5Settings(login=12345678, password=SecretStr('**********'))

# Accès explicite uniquement
mt5.initialize(password=settings.mt5.password.get_secret_value())

# ❌ Jamais dans le code
MT5_PASSWORD = "mypassword"
```

### Priorité des sources de secrets

```
1. Docker Secrets (/run/secrets/*) — production recommandée
2. Variables d'environnement (.env) — staging/dev acceptable
3. HashiCorp Vault — optionnel pour les secrets dynamiques (rotation)
4. AWS Secrets Manager / GCP Secret Manager — si cloud provider
```

### Rotation des secrets

| Secret | Fréquence de rotation | Procédure |
|---|---|---|
| JWT_SECRET | Trimestrielle | Génération + redéploiement (logout forcé) |
| DB_PASSWORD | Semestrielle | Migration Alembic + redémarrage |
| MT5 Password | Selon politique broker | Mise à jour .env + redémarrage |
| Telegram Token | Si compromis | Revoke + nouveau token BotFather |

---

## 5. Rate limiting et protection DDoS

### Rate limiting FastAPI

```python
# api/main.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

@router.post("/orders/")
@limiter.limit("10/minute")   # 10 ordres max par minute par IP
async def submit_order(request: Request, ...):
    ...

@router.post("/backtest/run")
@limiter.limit("5/minute")    # Backtests coûteux en CPU
async def run_backtest(request: Request, ...):
    ...
```

### nginx — rate limiting global

```nginx
# Limite les connexions par IP
limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;
limit_conn_zone $binary_remote_addr zone=conn:10m;

location /api/ {
    limit_req  zone=api burst=20 nodelay;
    limit_conn conn 10;
}

# WebSocket — connexions limitées séparément
location /ws/ {
    limit_conn conn 3;   # Max 3 WS connections par IP
}
```

### Headers de sécurité

```nginx
# Protection XSS, clickjacking, MIME sniffing
add_header X-Content-Type-Options   "nosniff" always;
add_header X-Frame-Options          "DENY" always;
add_header X-XSS-Protection         "1; mode=block" always;
add_header Content-Security-Policy  "default-src 'self'" always;
add_header Referrer-Policy          "strict-origin-when-cross-origin" always;
```

---

## 6. Sécurité des credentials broker

### Isolation MT5

```python
class MT5BrokerAdapter:
    def __init__(self, login, password, server):
        # Les credentials ne sont JAMAIS loggués
        self._login    = login
        self._password = password  # Non loggué
        self._server   = server

    def _mt5_connect(self):
        success = mt5.initialize(
            login    = self._login,
            password = self._password,   # passé à la DLL C++, jamais dans les logs
            server   = self._server,
        )
        if not success:
            error = mt5.last_error()
            # Log l'erreur SANS le mot de passe
            logger.error("MT5 connect failed: code=%d message=%s", *error)
```

### Principe du moindre privilège MT5

Créer un compte MT5 dédié à la plateforme avec des droits minimaux :
- Accès en lecture uniquement si possible
- Si trading requis : accès trading seulement, pas de gestion de compte
- Jamais utiliser le compte maître

---

## 7. Audit trail

### Table `audit_log` (append-only)

```sql
-- Les règles PostgreSQL empêchent toute modification
CREATE RULE audit_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;
```

### Événements audités

```python
# Tout ordre soumis
await audit.log(
    actor       = auth["sub"],
    action      = "ORDER_SUBMITTED",
    resource    = "order",
    resource_id = order.order_id,
    details     = {"symbol": order.symbol, "side": order.side, "qty": float(order.quantity)},
    ip_address  = request.client.host,
)

# Reset circuit breaker
await audit.log(
    actor   = auth["sub"],
    action  = "CIRCUIT_BREAKER_RESET",
    details = {"reason": "manual_reset", "previous_reason": cb_reason},
)

# Activation/désactivation stratégie
await audit.log(
    actor       = auth["sub"],
    action      = "STRATEGY_DEACTIVATED",
    resource    = "strategy",
    resource_id = strategy_id,
)
```

---

## 8. Sécurité des containers

### Utilisateur non-root

```dockerfile
# Le container tourne en tant qu'utilisateur non-privilégié
RUN useradd -r trading --uid=1001
USER trading    # ← Toujours la dernière instruction avant HEALTHCHECK/CMD
```

### Filesystem en lecture seule

```yaml
# docker-compose.yml
trading-core:
  read_only: true
  tmpfs:
    - /tmp          # Seul /tmp est writable
    - /run
```

### Pas de privilèges élevés

```yaml
# docker-compose.yml
trading-core:
  security_opt:
    - no-new-privileges:true   # Empêche l'escalade de privilèges
  cap_drop:
    - ALL                      # Supprimer toutes les capabilities Linux
  cap_add:
    - NET_BIND_SERVICE         # Seule exception si port < 1024
```

### Scan de vulnérabilités

```bash
# Trivy — scanner gratuit d'images Docker
trivy image trading-platform:latest

# Résultat attendu (objectif)
Total: 0 (CRITICAL:0, HIGH:0)
```

---

## 9. Checklist OWASP Top 10

| Risque OWASP | Statut | Mesure |
|---|---|---|
| A01 - Broken Access Control | ✅ | JWT RBAC sur tous les endpoints |
| A02 - Cryptographic Failures | ✅ | TLS 1.3, secrets hachés |
| A03 - Injection | ✅ | asyncpg parameterized queries |
| A04 - Insecure Design | ✅ | Séparation des responsabilités, DI |
| A05 - Security Misconfiguration | ⚠️ | Headers sécurité à vérifier |
| A06 - Vulnerable Components | ⚠️ | Trivy scan + `pip audit` réguliers |
| A07 - Auth Failures | ✅ | JWT + rate limiting |
| A08 - Integrity Failures | ✅ | Docker image signing à ajouter |
| A09 - Logging Failures | ✅ | JSON logs + audit trail |
| A10 - SSRF | ✅ | Pas de fetch d'URLs externes par l'utilisateur |

---

## 10. Réponse aux incidents

### Classification des incidents

| Niveau | Exemple | Réponse |
|---|---|---|
| P0 | Credentials broker compromis | Arrêt immédiat + rotation |
| P0 | Ordres non autorisés détectés | Circuit breaker + investigation |
| P1 | JWT secret potentiellement leak | Rotation + invalidation sessions |
| P2 | Rate limiting déclenché massivement | Analyse IP + blocage si malveillant |
| P3 | Tentatives d'injection SQL | Log + blocage IP |

### Procédure P0 — Credentials compromis

```bash
# 1. IMMÉDIATEMENT — Arrêter le trading
curl -X POST http://localhost:8000/api/v1/risk/circuit-breaker/reset
# → Non, on veut le laisser ouvert !
# → Désactiver toutes les stratégies

# 2. Révoquer les credentials sur le portail broker
# → Changer le mot de passe MT5 immédiatement

# 3. Arrêter la plateforme
docker compose down

# 4. Rotation des secrets
openssl rand -hex 32  # Nouveau JWT_SECRET
# Mettre à jour .env

# 5. Analyser les logs pour identifier la fuite
grep "ERROR\|CRITICAL" /var/log/trading.json | tail -200

# 6. Relancer avec les nouveaux credentials
docker compose up -d
```

---

*Document précédent → [10_DASHBOARD.md](10_DASHBOARD.md)*  
*Document suivant → [12_TESTING.md](12_TESTING.md)*
