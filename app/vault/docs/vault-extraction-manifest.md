# Vault extraction manifest

What leaves HighScoreServer when `app/vault/` becomes a standalone package, and what has to be
edited in HSS when it does.

This file exists so extraction is a checklist rather than an audit. A single manifest is more
durable than removability comments scattered across modules, which go stale silently.

## Moves with the package

| Path | Destination | Note |
| ---- | ----------- | ---- |
| `app/vault/` | package root | Intra-package imports are already relative, so this is a directory move, not a rewrite. `tests/vault/test_boundaries.py` enforces the property. |
| `app/vault/AGENTS.md` | package root | Already inside the package; needs no edit. |
| `app/vault/docs/` | package docs | Architecture, configuration runbook, extraction manifest, and the vault ADR lineage. |
| `app/vault/docs/adr/` | package docs | Independent lineage starting at 0001. Does not interleave with HSS's `docs/adr/`. |
| `tests/vault/` | package tests | Tests import absolutely (`app.vault.…`); this is the one place a find-and-replace is expected. |

### Outside the package, but still vault-owned

These are at the repository root rather than under `app/vault/`, so they are separate moves and
easy to overlook:

| Path | Note |
| ---- | ---- |
| `vault_migrations/` | The vault Alembic lineage, versioned in `vault.vault_alembic_version`. `env.py` imports `app.vault.settings`; that import needs repointing. |
| `alembic-vault.ini` | Points at `vault_migrations/`. |

## Dependencies that leave

| Package | Used by |
| ------- | ------- |
| `pgvector` | `app/vault/tables.py` only. Nothing in HSS imports it. |

`SQLAlchemy` is shared: the vault uses Core directly, and HSS needs it as Alembic's engine
layer. It stays in both.

An embedding provider client is **not** listed here — the provider decision is still open and
no adapter exists. When one is added it belongs in this table.

## HSS files that need editing at extraction

| File | Change |
| ---- | ------ |
| `app/main.py` | Remove vault lifespan wiring and any mounted vault routes. |
| `app/env.py` | Remove vault environment handling. |
| `.github/workflows/ci.yml` | Remove `alembic -c alembic-vault.ini upgrade head`, `VAULT_*` environment variables, and the pgvector-layered Postgres image if HSS no longer needs the extension. |
| `requirements.txt` | Remove `pgvector`. |
| `AGENTS.md` | Delete the block delimited by `<!-- BEGIN vault-context -->` / `<!-- END vault-context -->`. |
| `.env.example` | Remove the `VAULT_*` entries. |

## Verification after extraction

- `grep -rn "vault" --include="*.py" app/ tests/` in HSS returns nothing.
- HSS's test suite passes without the vault schema present.
- `alembic upgrade head` (leaderboard lineage) succeeds against a database with no `vault`
  schema.
