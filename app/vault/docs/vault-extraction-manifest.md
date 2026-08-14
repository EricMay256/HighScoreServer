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
| `scripts/seed_vault_demo.py` | Onboarding fixture loader. Lives in the host's `scripts/` by convention and imports `app.env` and `app.vault.*` absolutely; both imports need repointing on the move. |
| `scripts/measure_embedding_latency.py` | Provider latency measurement for the retry-budget decision. Same convention and the same absolute imports to repoint. |
| `scripts/measure_dedup_similarity.py` | Derives `Policy.flag_at` for the configured embedding model (ADR 0016 amendment, ADR 0017). Same convention and imports; also imports `app.vault.calibration`. |
| `scripts/issue_vault_credential.py` | Issues, lists, and revokes agent credentials. Same convention and imports. |
| `scripts/release.sh` | **Shared, not vault-owned.** Remove only the `VAULT_ENABLED`-gated `alembic -c alembic-vault.ini upgrade head` block; the leaderboard lineage stays. |

## Dependencies that leave

| Package | Used by |
| ------- | ------- |
| `pgvector` | `app/vault/tables.py` only. Nothing in HSS imports it. |

`SQLAlchemy` is shared: the vault uses Core directly, and HSS needs it as Alembic's engine
layer. It stays in both.

`slowapi` is shared: `app/vault/rate_limit.py` builds its **own** `Limiter` for the pre-auth
IP guard, and HSS has a separate one in `app/limiter.py`. Two independent instances, no
shared state, and neither imports the other — so extraction moves the vault's and leaves
HSS's alone. It stays in both. The vault repo must declare it; nothing else changes.

`httpx` is shared: the vault's embedding adapter uses it, and HSS uses it for Steam ticket
validation. It stays in both.

**No embedding client appears here.** Vault ADR 0005 selected OpenAI and deliberately called the
REST endpoint through `httpx` rather than the `openai` SDK, so the adapter added no package. If
a future provider needs a vendor SDK, it belongs in this table.

## HSS files that need editing at extraction

| File | Change |
| ---- | ------ |
| `app/main.py` | Remove vault lifespan wiring (`init_vault_db`, `init_vault_embeddings` and their `close_*` counterparts), the `vault_enabled()` route gate, and the `/api/v1/vault` router registration. The `load_environment()` call at the top of `create_app` exists so that gate can be evaluated — check whether anything else came to depend on it before removing it. |
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
