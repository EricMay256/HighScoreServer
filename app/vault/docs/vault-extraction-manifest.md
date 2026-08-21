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
| Vault-owned tests listed below | package tests | Package tests currently import `app.vault.…`; repoint those imports and provide the standalone fixtures described below. Do **not** move the directory wholesale. |

### Test ownership and replacement fixtures

| Owner | Current tests | Extraction action |
| ---- | ---- | ---- |
| Vault package | `test_audit_schema.py`, `test_auth.py`, `test_calibration.py`, `test_embedding_settings.py`, `test_embedding_text.py`, `test_embeddings_openai.py`, `test_export.py`, `test_facets.py`, `test_governance.py`, `test_origin.py`, `test_read_policy.py`, `test_repositories.py`, `test_reviews.py`, `test_search.py`, `test_settings.py`, `test_slug.py` | Move and repoint package imports. Replace the root `configure_test_env` dependency with a package-owned database URL fixture. |
| Private composition | `test_contributions.py`, `test_routes.py`, `test_rate_limit.py`, `test_schema_drift.py`, `tests/vault/conftest.py` | Move to the composing application or split package-only cases out. Replace HSS's global `TestClient(app.main.app)` with a standalone vault application factory; provide package-owned migrated Postgres/pgvector and credential fixtures. |
| HSS host | `test_hss_pool_config.py` | Keep in HSS and move out of `tests/vault/`; it verifies `app.db`, not the extracted package. |
| Dual-lineage composition | `test_migrations.py`, `migration_helpers.py` | Keep the shared-vs-separate topology cases with the composition owner. Move vault-only upgrade/downgrade, offline-render, and extracted revision-graph cases with the vault lineage. |
| Boundary contract | `test_boundaries.py` | Split: package-to-host and historical-migration checks move with the vault; the reverse host-to-vault scan stays in HSS. |

The standalone test harness must own: a minimal FastAPI composition root and
lifespan, a `TEST_DATABASE_URL`/engine fixture, disposable migrated PostgreSQL
databases with pgvector, credential issue/cleanup helpers, and deterministic
embedding providers. Importing fixtures back from HSS would recreate the
dependency this extraction is meant to remove.

### Outside the package, but still vault-owned

These are at the repository root rather than under `app/vault/`, so they are separate moves and
easy to overlook:

| Path | Note |
| ---- | ---- |
| `vault_migrations/` | The vault Alembic lineage, versioned in `vault.vault_alembic_version`. Historical revisions import only `vault_migrations.helpers`, a stable migration-owned module. Repoint `env.py` from `app.vault.settings` to the extracted runtime settings module. |
| `alembic-vault.ini` | Points at `vault_migrations/`. |
| `scripts/seed_vault_demo.py` | Onboarding fixture loader. Lives in the host's `scripts/` by convention and imports `app.env` and `app.vault.*` absolutely; both imports need repointing on the move. |
| `scripts/measure_embedding_latency.py` | Provider latency measurement for the retry-budget decision. Same convention and the same absolute imports to repoint. |
| `scripts/measure_dedup_similarity.py` | Derives `Policy.flag_at` for the configured embedding model (ADR 0016 amendment, ADR 0017). Same convention and imports; also imports `app.vault.calibration`. |
| `scripts/vault_load_probe.py` | Drives concurrent traffic and reports what the connection pool did, for the enablement pool review. Same convention and imports; also imports `app.vault.measurement`. |
| `scripts/issue_vault_credential.py` | Issues, lists, and revokes agent credentials. Same convention and imports. |
| `scripts/export_vault_markdown.py` | Projects the `Agent/` tree out as markdown (ADR 0022). Same convention and imports; also imports `app.vault.export`. |
| `scripts/release.sh` | **Shared, not vault-owned.** Remove only the `VAULT_ENABLED`-gated `alembic -c alembic-vault.ini upgrade head` block; the leaderboard lineage stays. |

## Dependencies that leave

| Package | Used by |
| ------- | ------- |
| `pgvector` | `app/vault/tables.py` only. Nothing in HSS imports it. |
| `mcp` | `app/vault/mcp.py` only. Nothing in HSS imports it. |
| `mcp-types` | Transitive under `mcp`. |
| `httpx2`, `httpcore2` | Transitive under `mcp`. **Not** the `httpx` HSS already uses — a second, independently versioned HTTP stack that installs alongside it rather than replacing it. Both remain resident while the vault is hosted here. |
| `opentelemetry-api` | Transitive under `mcp`. HSS's tracing is Sentry; nothing in the leaderboard emits OTel. |
| `truststore` | Transitive under `mcp`. |

Installing `mcp` also raised the pinned `idna` from 3.11 to 3.18. That one is genuinely
shared — `httpx`, `email-validator`, and `anyio` all reach it — so it does **not** leave with
the package, and the bump stays in HSS's manifest after extraction.

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
| `app/db.py` | Restore an HSS-only pool default/capacity decision and remove comments whose arithmetic reserves connections for the vault. Do not blindly restore 10 per worker; retain operational reserve. |
| `README.md` | Remove the staged-vault overview, extraction-cost table, dark-deployment notes, dual-lineage commands, and vault connection-budget discussion. |
| `db/role.sql` | Remove the vault-schema ownership/grant commentary; keep leaderboard role grants unchanged. |
| `docs/HANDOFF.md`, `docs/NEXT-STEPS.md` | Remove or archive vault task state and migration-head notes; these host documents do not move as package documentation. |
| `docs/adr/README.md` | Recheck host ADR index/count and remove any staging cross-reference; the independent vault ADR directory moves intact. |
| `pyproject.toml` | Remove vault-specific lint commentary and recalculate any file-count rationale after the package leaves. |
| `app/env.py` | **Retain.** It is HSS's general `.env` loader and contains no vault-specific parsing. Only reassess the early `create_app()` call noted under `app/main.py`. |
| `.github/workflows/ci.yml` | Remove `alembic -c alembic-vault.ini upgrade head`, `VAULT_*` environment variables, and the pgvector-layered Postgres image if HSS no longer needs the extension. |
| `requirements.txt` | Remove `pgvector`. |
| `AGENTS.md` | Delete the block delimited by `<!-- BEGIN vault-context -->` / `<!-- END vault-context -->`. |
| `.env.example` | Remove the `VAULT_*` entries. |

## Verification after extraction

- `grep -rn "vault" --include="*.py" app/ tests/` in HSS returns nothing.
- A broader host-only scan covers `README.md`, `db/`, `.github/`, `docs/`,
  `pyproject.toml`, `.env.example`, and `scripts/`; every remaining match is
  reviewed rather than assumed harmless.
- HSS's test suite passes without the vault schema present.
- `alembic upgrade head` (leaderboard lineage) succeeds against a database with no `vault`
  schema.
