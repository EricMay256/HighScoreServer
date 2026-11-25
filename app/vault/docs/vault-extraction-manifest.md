# Vault extraction manifest

What leaves HighScoreServer when `app/vault/` becomes a standalone package, and what has to be
edited in HSS when it does.

This file exists so extraction is a checklist rather than an audit. A single manifest is more
durable than removability comments scattered across modules, which go stale silently.

**Re-audited against `dev` on 2026-11-25.** Thirty test files, two scripts and two test assets
that had landed since the last revision are now classified. Destination and composition
follow vault ADR 0062; see "Destination and composition" below.

## Destination and composition (ADR 0062)

- **Destination:** a new private repository, `knowledge-vault` (package `knowledge_vault`),
  holding no corpus content.
- **It is the Heroku deploy root.** `high-score-server` is re-pointed to it. At build it fetches
  HSS's public `main`, which needs no credential, and it owns the composed `wsgi`, the Procfile
  and the release script.
- **First step, done inside HSS before anything moves: a generic extension point.** HSS's
  `create_app` accepts extensions. Each extension provides `include(app)`, called where the
  vault block sits today, ahead of the SPA catch-all, and a `lifespan(app)` async context,
  entered inside HSS's lifespan.

  `app/main.py` currently wires the vault in about 100 lines spread across `lifespan` and
  `create_app`: engine and embedding start-up, `report_vault_pool`, `vault_mcp_lifespan`, the
  REST router, the saturation handler, the MCP mount and its canonical-path redirect, and the
  OAuth, landing, browse and review routes. That wiring moves into a vault-owned extension
  (`app/vault/composition.py`), registered from `wsgi.py` behind `VAULT_ENABLED` until
  extraction. After extraction HSS's `wsgi.py` passes no extensions, and `knowledge-vault`'s
  passes the vault's.

## Moves with the package

| Path | Destination | Note |
| ---- | ----------- | ---- |
| `app/vault/` | package root | Intra-package imports are already relative, so this is a directory move, not a rewrite. This includes the MCP adapter, the consoles, and the Human routes and services (ADRs 0049–0052); `tests/vault/test_boundaries.py` enforces the property. |
| `app/vault/AGENTS.md` | package root | Already inside the package; needs no edit. |
| `app/vault/docs/` | package docs | Architecture, configuration runbook, extraction manifest, `invariants-rationale.md`, the answer-grounding specs and `evaluations/`, superseded plans kept for their reasoning, and the vault ADR lineage. |
| `app/vault/templates/` | package assets | Built 2026-08-21 (vault ADR 0024): the vault's first browser assets. Deliberately the vault's own rather than HSS's root `templates/`, with its own environment in `templating.py`: a page extending the host's `base.html` would not move with the package, and `test_boundaries.py` could not catch it because it scans imports rather than templates. |
| `app/vault/static/` | package assets | Versioned browser bundles and their licenses for the browse console. Served from the package-owned `/vault/assets/` mount; no host `public/` path or frontend build step is involved. |
| `app/vault/docs/adr/` | package docs | Independent lineage starting at 0001. Does not interleave with HSS's `docs/adr/`. |
| Vault-owned tests listed below | package tests | Package tests currently import `app.vault.…`; repoint those imports and provide the standalone fixtures described below. Do **not** move the directory wholesale. |

### Test ownership and replacement fixtures

| Owner | Current tests | Extraction action |
| ---- | ---- | ---- |
| Vault package | `test_body_diff.py`, `test_audit_schema.py`, `test_auth.py`, `test_calibration.py`, `test_credential_scopes.py`, `test_embedding_settings.py`, `test_embedding_text.py`, `test_embeddings_openai.py`, `test_export.py`, `test_facets.py`, `test_google_oidc.py`, `test_governance.py`, `test_hash_operator_password_script.py`, `test_human_boundary.py`, `test_oauth_store.py`, `test_origin.py`, `test_passwords.py`, `test_promotion.py`, `test_prune_oauth.py`, `test_remap_reference_ids.py`, `test_resolve_wikilinks_script.py`, `test_wiki_import.py`, `test_wikilinks.py`, `test_read_policy.py`, `test_repositories.py`, `test_reviews.py`, `test_search.py`, `test_settings.py`, `test_slug.py` | Move and repoint package imports. Replace the root `configure_test_env` dependency with a package-owned database URL fixture. |
| Private composition | `test_amendments.py`, `test_compile.py`, `test_contributions.py`, `test_mcp.py`, `test_oauth_flow.py`, `test_routes.py`, `test_rate_limit.py`, `test_schema_drift.py`, `tests/vault/conftest.py` | Move to the composing application or split package-only cases out. Replace HSS's global `TestClient(app.main.app)` with a standalone vault application factory; provide package-owned migrated Postgres/pgvector and credential fixtures. |
| Vault package, added 2026-11-25 (no database) | `test_browse_console.py`, `test_browse_propose_behaviour.py`, `test_chunk_eligibility_fences.py`, `test_console_session_module.py`, `test_console_style.py`, `test_describe_database.py`, `test_landing_page.py`, `test_listing_cursor.py`, `test_mcp_contract.py`, `test_measure_chunk_eligibility.py`, `test_measurement.py`, `test_pool_observer.py`, `test_public_url_drift.py`, `test_retrieval_cases.py`, `test_retrieval_harness.py`, `test_review_console.py`, `test_snippet.py`, `test_span_edit.py`, `test_wiki_import_revalidation.py`, plus the assets `console_harness.js` and `mcp_surface.json` | Move with the package. `test_browse_console.py` already builds its own Starlette app; none reaches `app.main`. |
| Vault package, added 2026-11-25 (database) | `test_backfill_summaries_script.py`, `test_metadata_amendments.py`, `test_oauth_grant_label.py` | Move, replacing `TEST_DATABASE_URL`/`configure_test_env` with the package-owned database fixture. |
| Private composition, added 2026-11-25 | `test_human_deletes.py`, `test_human_reads.py`, `test_human_writes.py`, `test_mcp_budget.py`, `test_note_listing.py`, `test_restore_human_note_script.py`, `test_span_edit_tool.py`, `test_summary_carveout.py` | All use the root `client` fixture, i.e. `TestClient(app.main.app)`. Run them in the private repository's CI against HSS's public `main` (ADR 0062), or rebase them onto a package-only app factory where they do not need the host. |
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
| `vault_migrations/` | The vault Alembic lineage, versioned in `vault.vault_alembic_version`, including `0021_human_collection_boundary` and any revisions ADRs 0058–0061 add. Historical revisions import only `vault_migrations.helpers`, a stable migration-owned module. Repoint `env.py` from `app.vault.settings` to the extracted runtime settings module. |
| `alembic-vault.ini` | Points at `vault_migrations/`. |
| `scripts/seed_vault_demo.py` | Onboarding fixture loader. Lives in the host's `scripts/` by convention and imports `app.env` and `app.vault.*` absolutely; both imports need repointing on the move. |
| `scripts/measure_embedding_latency.py` | Provider latency measurement for the retry-budget decision. Same convention and the same absolute imports to repoint. |
| `scripts/measure_dedup_similarity.py` | Derives `Policy.flag_at` for the configured embedding model (ADR 0016 amendment, ADR 0017). Same convention and imports; also imports `app.vault.calibration`. |
| `scripts/measure_chunk_eligibility.py` | Reports how much of the corpus would be eligible for retrieval chunking, and how many vectors that would add (vault ADR 0034's reopening trigger). Same convention and imports; also imports `app.vault.measurement` and `app.vault.read_policy`. Needs no API key. |
| `scripts/measure_retrieval_quality.py` | Runs the labelled query set in `app/vault/retrieval_cases.py` against live search and reports recall@k and MRR by case category (vault ADR 0034's fourth trigger). Same convention and imports; also imports `app.vault.retrieval_cases`. |
| `scripts/vault_load_probe.py` | Drives concurrent traffic and reports what the connection pool did, for the enablement pool review. Same convention and imports; also imports `app.vault.measurement`. |
| `scripts/issue_vault_credential.py` | Issues, lists, and revokes agent credentials; adjusts static scopes and persistent OAuth-family entitlements (`grant-oauth` / `revoke-oauth-scope`, vault ADR 0029). Same convention and imports. |
| `scripts/import_vault_wiki.py` | One-off: imports the Stage-A `Agent/wiki/` pages as `kind='wiki'` rows (ADR 0027). Same convention and imports. Retire it once it has run. |
| `scripts/remap_vault_reference_ids.py` | Repairs source and related note ids after one or more service import generations. Dry-run by default; composes import maps and refuses ambiguous identity classes. |
| `scripts/resolve_vault_wikilinks.py` | Repairs `related_ids` rows holding `[[Wikilink]]` strings instead of ids (vault ADR 0025's 2026-08-26 amendment). Dry-run by default; preserves the original list in `frontmatter` before rewriting, and reports rather than guesses an ambiguous name. Same convention and imports; also imports `app.vault.wikilinks`. |
| `scripts/backfill_vault_summaries.py` | Backfills authored `summary` values onto notes contributed without one (vault ADR 0035). Plans and emits a work file, an agent authors it, apply re-embeds and writes. Dry-run by default; skips the dedup gate deliberately and owns `embedded_text_sha256` itself. Same convention and imports; also imports `app.vault.governance` and `app.vault.embedding_text`. |
| `scripts/prune_vault_oauth.py` | Prunes expired OAuth state and long-revoked OAuth credentials (ADR 0024). Same convention and imports. |
| `scripts/hash_vault_operator_password.py` | Prints the bcrypt hash for `VAULT_OPERATOR_PASSWORD_HASH` (ADR 0024). Same convention; imports `app.vault.passwords` and nothing else, so it needs no database and only that one import repointed. |
| `scripts/export_vault_markdown.py` | Projects the `Agent/` tree out as markdown (ADR 0022). Same convention and imports; also imports `app.vault.export`. |
| `scripts/check_read_policy_parity.py` | Diffs `ai_read` in the private `folders.yml` against `READABLE_PATH_PREFIXES` (ADR 0048 phase A). Same convention; imports `app.vault.read_policy` and nothing else, so it needs no database and only that one import repointed. The governance file it reads stays in knowledge-platform and is passed by path, so nothing about it moves. |
| `scripts/restore_human_note.py` | Operator-only restore of a tombstoned Human note under its own id (ADR 0052). Same convention and imports. |
| `scripts/validate_body_diff.py` | Client-side validator for strict unified body diffs (ADR 0033's companion). Same convention; imports `app.vault.body_diff`. |
| `scripts/release.sh` | **Shared, not vault-owned.** Remove only the `VAULT_ENABLED`-gated `alembic -c alembic-vault.ini upgrade head` block; the leaderboard lineage stays. The composed app's release script in `knowledge-vault` runs both. |

## Dependencies that leave

The package also carries two vendored browser dependencies below
`app/vault/static/vendor/`: Marked 18.0.11 and DOMPurify 3.4.15, with their
license texts and source maps. They move as package assets and require no npm
installation or lockfile.

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
validation. Google OIDC also uses it for the authorization-code exchange and JWKS fetch. It
stays in both.

`python-jose` and `cryptography` are shared: HSS verifies leaderboard JWTs with them, while
`app/vault/google_oidc.py` verifies Google's RS256 ID tokens against its JWKS. They stay in
both, and the extracted vault declares them directly.

`Jinja2` is shared as of ADR 0024's login page: HSS renders its Jinja2 views from a root
`templates/` directory and the vault renders its own from `app/vault/templates/` through
`app/vault/templating.py`, with separate environments and no shared base template. Two
independent users of one library, like `slowapi` — it stays in both, and the vault repo must
declare it.

`bcrypt` is shared as of ADR 0024's operator login: `app/vault/passwords.py` hashes the operator
password with it, and `app/auth.py` hashes user passwords. Two independent users again, and the
duplication is deliberate — `app/vault/` may contain no `from app.`, so the vault carries its own
ten-line wrapper rather than a host dependency the package could not take with it. It stays in
both, and the vault repo must declare it.

**No embedding client appears here.** Vault ADR 0005 selected OpenAI and deliberately called the
REST endpoint through `httpx` rather than the `openai` SDK, so the adapter added no package. If
a future provider needs a vendor SDK, it belongs in this table.

## HSS files that need editing at extraction

| File | Change |
| ---- | ------ |
| `app/main.py`, `wsgi.py` | Keep the generic extension point; `wsgi.py` stops registering the vault extension. Nothing in HSS names the vault package. What the extension replaced: vault lifespan wiring (`init_vault_db`, `init_vault_embeddings` and their `close_*` counterparts), the `vault_enabled()` route gate, and the `/api/v1/vault` router registration. The `load_environment()` call at the top of `create_app` exists so that gate can be evaluated — check whether anything else came to depend on it before removing it. |
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

## Offline grounding evaluation assets

Move `scripts/evaluate_vault_grounding.py`, `scripts/run_vault_grounding_comparison.py`,
`tests/vault/test_grounding_evaluation.py`, `tests/vault/test_grounding_comparison.py`, and
`tests/vault/fixtures/grounding/cases.json` with the vault. The runbook and specs
already live under `app/vault/docs/`. Move the dedicated offline grounding CI step
out of HSS and into the extracted package's CI; adjust its module paths and the
checker's default fixture path if the destination layout differs. This evaluator
uses Pydantic but imports no host application or database modules.

## Verification after extraction

- `grep -rn "vault" --include="*.py" app/ tests/` in HSS returns nothing.
- A broader host-only scan covers `README.md`, `db/`, `.github/`, `docs/`,
  `pyproject.toml`, `.env.example`, and `scripts/`; every remaining match is
  reviewed rather than assumed harmless.
- HSS's test suite passes without the vault schema present.
- `alembic upgrade head` (leaderboard lineage) succeeds against a database with no `vault`
  schema.
