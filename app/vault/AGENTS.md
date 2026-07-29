# AGENTS.md — vault bounded context

Standing context for work under `app/vault/` and `vault_migrations/`. This file travels with
the package; the host repository's `AGENTS.md` governs everything outside these directories.

## What this is

The knowledge-platform bounded context: its own API models, domain records, Core tables,
repositories, services, auth, embeddings, HTTP routes, and MCP adapter. It holds runtime code
and schema definitions only — never corpus content, credentials, exports, or vectors.

It is currently hosted inside HighScoreServer, which is a staging location rather than
permanent ownership. See `docs/vault-extraction-manifest.md` for what leaves and what has to
be edited when it does.

## Persistence

- **SQLAlchemy 2.x Core, never the ORM.** No `DeclarativeBase`, `Session`, `relationship`,
  `mapped_column`, or `sessionmaker`. The host repo's raw-SQL ADR does not apply here; see
  `docs/adr/0001-sqlalchemy-core-for-vault-bounded-context.md`.
- Pydantic API models, domain records, Core tables, repositories, and services stay separate
  layers. Public request/response models are deliberately not database records.
- Repositories take an `AsyncConnection` as their first parameter and never acquire one
  themselves. Services own transactions.
- Core metadata exists for query construction and schema-drift tests. It never replaces
  Alembic and must never reach `create_all()` in production.
- Migrations are explicit reviewed SQL in `vault_migrations/`, a lineage separate from the
  host's, versioned in `vault.vault_alembic_version`.
- `pgvector` is used only here and leaves with the package.

## Isolation

- No imports between vault code and host leaderboard routes, models, repositories, or
  tables, in either direction. `tests/vault/test_boundaries.py` enforces this.
- Intra-package imports are **relative** (`from .constants import ...`). `app/vault/` must
  contain no `from app.` or `import app.` — that is what keeps extraction a directory move
  instead of a find-and-replace, and it is also asserted by `test_boundaries.py`.
- No cross-schema foreign keys, views, triggers, shared sequences, or cross-domain
  transactions. Everything lives in the qualified `vault` schema.
- Documentation and ADRs live in `app/vault/docs/`, never in the host's `docs/`. The ADR
  lineage here is independent and starts at 0001.

## Schema invariants

- **Audit events carry correlation identifiers, not foreign keys.** `vault_audit_events` has
  no FK to `vault_write_requests`: an audit insert must never fail on a referential
  constraint, events for rejected or unauthenticated writes must keep their idempotency key,
  and write requests must remain prunable. `target_type`/`target_id` follow the same rule.
  `latency_ms` is nullable because lifecycle events have no meaningful latency. See ADR 0002.
- **Embeddings live in `vault_document_embeddings`, keyed by `(document_id, profile_id)`** —
  not as columns on `vault_documents`. "Not embedded" is the absence of a row. Re-embedding a
  profile is an upsert. See ADR 0003.
- **`VAULT_TEXT_SEARCH_CONFIG` is a migration-time choice.** It is compiled into the
  persisted `search_vector` generated column, so changing it later requires a table rewrite
  and a GIN reindex, not a restart. A startup assertion compares the environment against the
  expression actually stored in the catalog. Search queries must use the same configuration:
  `websearch_to_tsquery(:config, :query)`.
- Contribution policy for the write path is **not** implemented here during the read-only
  phase. `vault_contrib.core.decide()` and `vault_contrib.models.Policy` remain normative and
  will be ported verbatim with their tests at switchover. See ADR 0004.

## Retrieval and embeddings

- The embedding **port** (`embeddings.py`) names no vendor. Adapters live in their own modules
  (`embeddings_openai.py`), and `embedding_runtime.py` is the only place that maps a provider
  name to an adapter. Nothing may import an adapter from the port.
- `settings.py` deliberately does **not** validate that an adapter exists for the configured
  provider. That separation keeps the Alembic environment free of transport imports; the
  registry raises instead.
- The embedding provider is **optional at runtime**. Without a credential the vault serves
  lexical-only search and says so in the response. Do not turn a missing key back into a
  startup failure — CI depends on this.
- `vector_status` distinguishes `used` / `not_configured` / `failed`. Keep those three
  separate: a broken provider must never be reportable as a deliberate lexical-only
  deployment. `failed` also logs at ERROR; `not_configured` logs nothing per request.
- Never log the query text or an embedding exception's message — both can carry user content.
  Log the exception *type* instead. `tests/vault/test_search.py` asserts this.
- Search must pass the text search configuration as a bound parameter —
  `websearch_to_tsquery(:config, :query)` — never the database default, never interpolation.
- The first profile is `openai/text-embedding-3-small:1536` (ADR 0005); the two arms combine
  by Reciprocal Rank Fusion (ADR 0006). Changing provider or model is a controlled re-embed,
  never a credentials-only config change.

## Working agreements

- Type hints on every function signature; no module-level side effects.
- Any value interpolated into DDL or SQL is validated first — treat it as an injection
  surface even when it comes from a trusted operator.
- Flag anything needing an Alembic revision rather than only changing code.
- Flag new dependencies explicitly; anything added here must be listed in the extraction
  manifest as leaving with the package.
- Material architectural decisions get a Nygard-format ADR in `docs/adr/`, continuing this
  lineage's own numbering.
