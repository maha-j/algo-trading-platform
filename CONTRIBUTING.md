# Guide de contribution

## Processus

1. **Fork** le repository
2. **Branch** : `git checkout -b feature/ma-fonctionnalite`
3. **Tests** : `poetry run pytest tests/unit/ -v` (tous doivent passer)
4. **Lint** : `poetry run ruff check . && poetry run mypy .`
5. **Commit** : suivre le format Conventional Commits
6. **PR** : décrire la motivation et les changements

## Convention de commits

```
feat(risk): add CorrelationValidator to chain
fix(portfolio): correct commission double-deduction in apply_fill
docs(api): add WebSocket authentication examples
test(backtest): add walk-forward optimization coverage
refactor(events): rename BarEvent fields to OHLCV standard
```

## Standards de code

- **Type hints** complets sur toutes les signatures
- **Docstrings** Google style sur toutes les classes et méthodes publiques
- **Decimal** pour toutes les valeurs monétaires (jamais float)
- **datetime.now(timezone.utc)** pour les timestamps (jamais utcnow)
- **Frozen dataclasses** pour les événements domaine
- **asyncio** pour toute I/O — jamais de blocking dans la boucle

## Tests

Chaque PR doit maintenir 100% de passage des tests existants
et ajouter des tests pour toute nouvelle fonctionnalité.
