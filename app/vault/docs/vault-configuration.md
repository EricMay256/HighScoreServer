# Vault configuration and Heroku operations

**Status:** Phase 1 persistence-foundation runbook

**Initial topology:** one Essential-0 PostgreSQL database, `public` and `vault`
schemas

**Contains secrets:** no

This document records variable names and operator commands only. Never paste
real database URLs, tokens, note content, exports, or embedding vectors into
this file, source control, CI logs, or build artifacts.

## Phase 1 behavior

The persistence foundation is feature-gated. `VAULT_ENABLED=false` leaves the
vault engine closed and preserves the existing HSS request path. Phase 1 does
not add routes, credentials, embedding providers, import jobs, or deployment
changes.

The production Essential-0 plan permits 20 connections. With two Gunicorn
workers, the approved initial allocation is:

```text
(HSS pool max 5 + vault pool size 1) * 2 workers
  + 2 release/operator connections
  = 14 allocated

20 total - 14 allocated = 6 unallocated (30%)
```

The 30% remainder satisfies the architecture's requirement to leave at least
25% unallocated.

## Non-secret Heroku configuration

Do not apply these settings as part of Phase 1 development. Apply them in the
reviewed release that first enables the vault runtime:

```powershell
heroku config:set `
  HSS_DB_POOL_MIN_SIZE=1 `
  HSS_DB_POOL_MAX_SIZE=5 `
  HSS_PROCESS_COUNT=2 `
  DATABASE_CONNECTION_LIMIT=20 `
  DB_OPERATIONAL_CONNECTION_RESERVE=2 `
  VAULT_DB_POOL_SIZE=1 `
  VAULT_DB_POOL_TIMEOUT_SECONDS=5 `
  VAULT_TEXT_SEARCH_CONFIG=english `
  VAULT_ENABLED=true `
  --app high-score-server
```

The `Procfile` currently fixes Gunicorn at two workers. If that count changes,
update `HSS_PROCESS_COUNT` in the same release and recalculate the budget before
deploying.

## Text search configuration

```
# Baked into the generated search_vector column at migration time.
# Changing this after migration requires a table rewrite, not a restart.
VAULT_TEXT_SEARCH_CONFIG=english
```

`search_vector` is a **persisted generated column**. Its expression must be
`IMMUTABLE` and is compiled into DDL when the migration runs, so this variable is
read at migration time, not at runtime. An environment change cannot retune the
language of an existing corpus — that needs a table rewrite plus a GIN reindex.

The migration validates the value twice before it reaches DDL: against
`^[a-z_][a-z0-9_]*$`, and against `pg_catalog.pg_ts_config` in the target
database. A name PostgreSQL does not recognise aborts the migration rather than
silently producing a schema nobody asked for.

At startup the vault reads the expression actually stored in the catalog
(`pg_get_expr` over `pg_attrdef`, for the column where `attgenerated = 's'`) and
refuses to boot if it disagrees with the environment. Without that check a
mismatch would be silent: queries parsed with one configuration, stored vectors
and their index built with another.

Set this in the same release that first runs `alembic -c alembic-vault.ini
upgrade head`. Changing it later is a migration, not a config change. CI pins it
explicitly so the schema-drift test compares against a fixed target.

Query paths must use the same configuration —
`websearch_to_tsquery(:config, :query)` — rather than relying on the database's
`default_text_search_config`.

## Shared-database URL

The initial topology uses the existing Heroku-managed `DATABASE_URL`. Leave
`VAULT_DATABASE_URL` and `VAULT_DATABASE_CONNECTION_LIMIT` unset:

```powershell
heroku config:unset `
  VAULT_DATABASE_URL `
  VAULT_DATABASE_CONNECTION_LIMIT `
  --app high-score-server
```

The vault engine and vault Alembic environment then fall back to
`DATABASE_URL`. Do not copy the value returned by `heroku config:get
DATABASE_URL` into a tracked file.

## Secrets

Phase 1 introduces no new application secret.

- `DATABASE_URL` already contains a credential and remains managed by Heroku.
- `API_KEY` and `JWT_SECRET` remain existing HSS secrets.
- Agent bearer-token secrets and embedding-provider keys belong to later
  reviewed phases. Store them only in Heroku config when those phases define
  their exact names and rotation procedures.
- Never use `heroku config` output in CI logs or documentation.

Local secrets belong in `.env`, which is already ignored by Git. `.env.example`
contains placeholders and non-secret defaults only.

## Migration lineages

The leaderboard lineage owns `public.*` and records its revision in
`public.alembic_version`:

```powershell
alembic upgrade head
```

The vault lineage owns `vault.*` and records its revision in
`vault.vault_alembic_version`:

```powershell
alembic -c alembic-vault.ini upgrade head
```

Phase 1 does not change the Heroku release command. Before the first vault
deployment, update the release phase in a separately reviewed deployment slice
so it runs both commands sequentially and aborts if either fails.

Vault migrations may enable the database-wide `vector` extension, but they
never import Markdown, generate embeddings, or read the private
knowledge-platform repository.

## Pre-deployment verification

After authenticating the Heroku CLI:

```powershell
heroku pg:info --app high-score-server
heroku pg:psql --app high-score-server
```

Run these read-only SQL checks in `psql`:

```sql
SELECT current_setting('max_connections');

SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name = 'vector';

SELECT version_num FROM public.alembic_version;
SELECT version_num FROM vault.vault_alembic_version;
```

Expected production facts:

- plan: Essential-0;
- connection limit: 20;
- `vector` is available;
- the two version tables contain heads from different Alembic lineages.

## Later move to a separate database

Provisioning or attaching another database is deliberately outside Phase 1.
When approved later:

1. Set `VAULT_DATABASE_URL` to the second database credential.
2. Set `VAULT_DATABASE_CONNECTION_LIMIT` to that plan's actual limit.
3. Recalculate the leaderboard and vault budgets independently.
4. Run the vault Alembic lineage against the new URL.
5. Move data through the reviewed schema-qualified export/restore procedure.

No route, repository, table definition, or transaction may assume that the
vault and leaderboard share a database.

## Extraction note

The `pgvector` Python package is required only by `app/vault/`. When the vault
runtime moves into the private knowledge-platform package, move that dependency
with it and remove it from HSS's manifest. Existing leaderboard code must not
import it.

The `mcp` and embedding-provider dependencies are also vault-only dependencies.
Move them with the package during extraction and remove them from HSS.

## Changing embedding model or dimensions

Embeddings live in `vault.vault_document_embeddings`, keyed by
`(document_id, profile_id)` — see vault ADR 0003. That changes what a re-embed
costs, and the two cases are no longer the same operation.

### Same dimensions, different provider or model

**No migration.** The new profile is new rows. Both profiles coexist, so the old
one stays queryable for rollback and the two can be compared directly.

1. Select and evaluate the replacement profile. Record provider, exact
   model/revision, query/document modes, dimensions, and normalization
   behavior. Re-run retrieval fixtures: vectors from different profiles are not
   comparable even when their dimensions match.
2. Run a resumable operator backfill outside the release phase. Embed documents
   in bounded batches under the new `profile_id`, checkpointing progress without
   logging note bodies, credentials, or vectors. `upsert` is keyed on the
   primary key, so the job is safe to re-run.
3. Add a partial HNSW index for the new profile
   (`WHERE profile_id = '<literal>'`) before serving reads from it. The base
   index is unpartitioned, so with two populated profiles a profile filter is a
   post-filter and can cost recall.
4. Verify every searchable active document has a row under the new profile.
   Check counts, stable document IDs, exact-token and paraphrase fixtures,
   top-10 overlap, latency, and embedding failure metrics.
5. Switch query embedding and retrieval to the new profile in a separately
   reviewed release. Keep lexical retrieval active and retain the old profile's
   rows for rollback.
6. After the rollback window, delete the old profile's rows and drop its partial
   index. This is a `DELETE` plus a `DROP INDEX`, not a table rewrite.

### Different dimensions

`VAULT_EMBEDDING_DIMENSIONS` remains a checked deployment contract, not a
setting that resizes a pgvector column. At startup it must match the dimension
compiled into the current Core metadata and created by Alembic; changing it
alone intentionally fails fast.

`vault_document_embeddings.embedding` is `vector(1536)`, and HNSW requires a
fixed dimension, so a dimension change still requires reviewed DDL — the join
table removes the migration for model swaps, not for dimension swaps.

**The DDL shape for this is an open decision** and should be settled when a
dimension change is actually proposed rather than guessed at now. The plausible
options are a second vector column on the same table, or a parallel table per
dimension; which is better depends on whether the two dimensions need to be
served concurrently. Whichever is chosen: keep the historical migration literal
unchanged, do not alter a populated column in place, and never call an embedding
provider from Alembic.

For a small local or disposable database, dropping the rows and re-embedding is
acceptable. Production must use the additive path so a model change never
requires an irreversible release migration.

## Follow-up: alternative embedding providers

Before the read-only production rollout, evaluate at least one managed
alternative and one open-weight/self-hostable option against the same fixed
retrieval corpus. Compare retrieval quality, 1536-dimension support, query vs
document modes, latency, batch limits, data-retention terms, regional
availability, cost, rate limits, and operational burden.

No provider adapter ships yet, and the provider decision is still open. When one
is added, the application-facing `EmbeddingProvider` protocol must live in a
separate module from any vendor adapter, so the vendor import is removable
without touching the port. Provider-specific request fields belong in adapters,
and `vault_document_embeddings.profile_id` carries the stable profile identity
(provider, model, and dimensionality together).

Changing provider or model requires controlled re-embedding; it must never be
treated as a credentials-only configuration change.
