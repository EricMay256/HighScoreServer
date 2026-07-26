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
  VAULT_ENABLED=true `
  --app high-score-server
```

The `Procfile` currently fixes Gunicorn at two workers. If that count changes,
update `HSS_PROCESS_COUNT` in the same release and recalculate the budget before
deploying.

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
