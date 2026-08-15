# Vault configuration and Heroku operations

**Status:** Phase 1 persistence-foundation runbook

**Initial topology:** one Essential-0 PostgreSQL database, `public` and `vault`
schemas

**Contains secrets:** no

**Nothing in this document has been applied. As of 2026-08-14 no `VAULT_*`
variable is set on `high-score-server`, and the vault has never been deployed.**
Read every command here as a plan, not as a description of the running app. The
vault ships dark by design — `VAULT_ENABLED` defaults to false, so no routes are
registered, no engine is created, and `scripts/release.sh` skips the vault
migration lineage — which is why the code can merge to `main` well before any of
this is configured. Confirm the real state with `heroku config --app
high-score-server` before acting on anything below.

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
(HSS pool max 4 + vault pool size 2) * 2 workers
  + 2 release/operator connections
  = 14 allocated

20 total - 14 allocated = 6 unallocated (30%)
```

The 30% remainder satisfies the architecture's requirement to leave at least
25% unallocated.

**The split moved from 5/1 to 4/2 on 2026-08-14, at the same total.** A vault
request checks out twice in sequence — once to authenticate, once to serve — so
at pool size 1 a second concurrent request waited out `pool_timeout` and failed:
the surface could not serve two callers at once. HSS gave up its fifth
connection to pay for it, because 5 was never measured (it is the original
default in `app/db.py`) whereas the vault needing two is measured in its own
timeout. `validate_connection_budget` passes by exactly one connection either
way, so **both variables must move together** — setting only
`VAULT_DB_POOL_SIZE=2` gives 16 against an available 15 and raises `RuntimeError`
at lifespan, taking the leaderboard down with the vault.

Two consumers this formula does not count. The release dyno takes one connection
for `alembic upgrade head` while the web dynos are still up (14 + 1 = 15 — inside
the hard limit, but spending the reserve). And preboot doubles every per-worker
figure, so `heroku features:enable preboot` needs the pool sizes halved first or
new dynos fail to boot mid-deploy.

## Non-secret Heroku configuration

Do not apply these settings as part of Phase 1 development. Apply them in the
reviewed release that first enables the vault runtime:

```powershell
heroku config:set `
  HSS_DB_POOL_MIN_SIZE=1 `
  HSS_DB_POOL_MAX_SIZE=4 `
  HSS_PROCESS_COUNT=2 `
  DATABASE_CONNECTION_LIMIT=20 `
  DB_OPERATIONAL_CONNECTION_RESERVE=2 `
  VAULT_DB_POOL_SIZE=2 `
  VAULT_DB_POOL_TIMEOUT_SECONDS=5 `
  VAULT_EMBEDDING_TIMEOUT_SECONDS=5 `
  VAULT_TEXT_SEARCH_CONFIG=english `
  VAULT_ENABLED=true `
  --app high-score-server
```

`VAULT_EMBEDDING_TIMEOUT_SECONDS` is listed here, not only with the other
embedding settings below, because it is **per attempt** and a plausible-looking
value silently exceeds Heroku's router budget. Read
"[`VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt, not per
request](#vault_embedding_timeout_seconds-is-per-attempt-not-per-request)"
before changing it. If it is already set on the app to anything above 7.3, this
release will refuse to boot until it is corrected — check first:

```bash
heroku config:get VAULT_EMBEDDING_TIMEOUT_SECONDS --app high-score-server
```

The `Procfile` currently fixes Gunicorn at two workers. If that count changes,
update `HSS_PROCESS_COUNT` in the same release and recalculate the budget before
deploying.

**Do not replace `-w 2` with `-w ${WEB_CONCURRENCY}` to remove that coupling.**
Heroku's Python buildpack sets `WEB_CONCURRENCY` in the dyno environment at boot
as `min(cores * 2 + 1, RAM_MB / 256)`, and it never appears in `heroku config`
because it is not a config var. Wiring it would make the connection budget a
function of dyno size: a 1 GB dyno yields 4 workers and 26 allocated against a
ceiling of 15, so resizing the dyno would stop the app booting. The Node.js
buildpack writes the same `.profile.d` filename, so with multiple buildpacks the
value would also depend on buildpack order, which this repo does not pin.
Gunicorn's own default for `workers` *is* `WEB_CONCURRENCY`, which is precisely
why `-w` is passed explicitly.

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
`default_text_search_config`. The lexical arm then disjoins that query's terms
before matching, so a long query does not require every term to be present; see
[ADR 0007](adr/0007-lexical-arm-disjoins-query-terms.md).

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

Phase 1 introduced no new application secret. The read-only slice introduces
two, both listed below.

- `DATABASE_URL` already contains a credential and remains managed by Heroku.
- `API_KEY` and `JWT_SECRET` remain existing HSS secrets.
- `VAULT_EMBEDDING_API_KEY` is introduced by the read-only slice. Store it only
  in Heroku config, never in a tracked file, and never echo it in CI logs. It is
  not logged by the application: the embedding adapter logs status codes only,
  and request bodies are never logged because they carry note content.
- Read access needs **no** environment secret. Agent credentials live in
  `vault.vault_agent_credentials` and are issued with
  `scripts/issue_vault_credential.py`; only the SHA-256 of each secret is
  stored. See vault ADR 0015.
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

The Heroku release phase is `bash scripts/release.sh`. It runs the leaderboard
lineage unconditionally, then the vault lineage **only when `VAULT_ENABLED` is
`true`**, and aborts the release if either fails.

The gate is deliberate. `0001_vault_foundation` runs `CREATE EXTENSION vector`;
if pgvector is unavailable on the attached plan, an ungated release phase would
abort *every* deploy, including ones unrelated to the vault. Because setting
`VAULT_ENABLED=true` itself triggers a release, the cutover is exactly when the
vault schema is built and a failure aborts that release rather than an
unrelated one. **Verify pgvector on the target plan before flipping the flag:**

```bash
heroku pg:psql --app <app> -c   "SELECT name, installed_version FROM pg_available_extensions WHERE name='vector';"
```

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

The embedding adapter adds no package: it uses `httpx`, which HSS already
depends on for Steam ticket validation, so `httpx` stays in both manifests. An
`mcp` dependency is not present — the read-only transport is HTTP only, and MCP
remains unapproved.

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

## Embedding provider

The provider evaluation is complete and the decision is recorded in vault ADR
0005. The first profile is **`openai/text-embedding-3-small:1536`**, reached
through the REST endpoint over `httpx` rather than the vendor SDK, so no new
package was added.

```
VAULT_EMBEDDING_PROVIDER=openai
VAULT_EMBEDDING_MODEL=text-embedding-3-small
VAULT_EMBEDDING_PROFILE_ID=openai/text-embedding-3-small:1536
VAULT_EMBEDDING_DIMENSIONS=1536
VAULT_EMBEDDING_TIMEOUT_SECONDS=5
# VAULT_EMBEDDING_BASE_URL=   # unset: each adapter supplies its own default
# VAULT_EMBEDDING_API_KEY=    # secret; Heroku config only, never a tracked file
```

### `VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt, not per request

**This is the easiest value here to get wrong, and it was wrong in this file,
in `.env.example`, and in the deployed configuration until 2026-08-14.** The
embedding call retries, so the number an operator sets is not the number that
bounds the request:

```text
worst case = 3 attempts x <timeout> + 2 x 4s of capped backoff
```

| Setting | Worst case | Outcome |
| --- | --- | --- |
| **5 (default)** | **23s** | Fits, with 7s left for the rest of the request |
| 7.3 | 29.9s | The largest value that fits |
| 10 | 38s | Router 503s at 30s; the work is discarded |

**Startup now refuses anything whose budget exceeds 30s**, and the error prints
the arithmetic, the maximum that fits, and what to do instead. Before this guard
existed the only check was a unit test against the *default* constant, which
kept passing while the deployed value was 10 — a test cannot see what a
deployment configures.

Do not raise this to give slow calls more room. 5s is roughly **four times** the
measured single-query p99 of 1.194s, and because the timeout is per attempt a
slow call is *retried* rather than lost. The failure this budget exists to
survive is a transient 429 or 502, not slowness.

A batch backfill is the one legitimate reason to want longer, and it must not
change this variable: it has no caller waiting, so it passes `timeout_seconds`
to the provider directly. Only the environment is constrained.

Changing it means changing it in every place it is set — `.env`, `.env.example`,
the `heroku config:set` block above, and this table — because nothing reconciles
them.

`VAULT_EMBEDDING_PROFILE_ID` defaults to `{provider}/{model}:{dimensions}` and
is validated at startup against the same pattern as the
`vault_document_embeddings_profile_id_format` check constraint, so a typo fails
the boot rather than the first insert.

**`VAULT_EMBEDDING_API_KEY` is optional by design.** Without it the vault runs
lexical-only: startup logs a warning, `profile_id` is reported as null, and
every response carries `vector_status: "not_configured"`. CI and local
development run this way. Setting `VAULT_EMBEDDING_PROVIDER` to a name with no
adapter is a different case and fails loudly.

A configured provider that then fails is a third case and is reported as
`vector_status: "failed"`, with an ERROR logged carrying the exception type and
the profile — never the query text or the exception message, both of which can
quote note content. **Treat `"failed"` as an alert condition:** results are
silently narrower than they should be, and nothing else will tell you.

Layering choice is separate from configuration: `settings.py` parses these
variables and deliberately does **not** know which adapters exist, which is what
lets the Alembic environment import it without pulling in an HTTP client. The
registry in `embedding_runtime.py` is the only module that maps a provider name
to a concrete adapter.

Changing provider or model requires controlled re-embedding; it must never be
treated as a credentials-only configuration change. The procedure is above under
"Changing embedding model or dimensions".

## Read-only access

The read surface is gated on operator-issued agent credentials, sent as
`Authorization: Bearer hssv1_<credential-id>_<secret>`:

```bash
python -m scripts.issue_vault_credential issue --name claude-code --scopes vault:read
python -m scripts.issue_vault_credential list
python -m scripts.issue_vault_credential revoke --id <credential-id>
```

Only the SHA-256 of each secret is stored, so the token is printed once and a
lost one is revoked and reissued rather than recovered. Issuing against
production means setting `DATABASE_URL` explicitly for the command — writing a
credential into the wrong database is silent.

The vault cannot reuse HighScoreServer's authentication — importing it would
breach the isolation rule that keeps extraction a directory move — and the
integration spec is explicit that player JWTs and the leaderboard `API_KEY` are
not vault credentials. A request with no credential, an unknown credential, or
a revoked or expired one is `401`; one that authenticates but lacks
`vault:read` is `403`. See vault ADR 0015.

Routes are registered only when `VAULT_ENABLED` is true, so a disabled vault
publishes no endpoints and no OpenAPI schema. They are mounted under
`/api/v1/vault`, ahead of the SPA catch-all and the static-file mount.

Rate limiting is **two layers**, both in `app/vault/rate_limit.py`.

The **quota** is enforced per authenticated principal by a vault-local token
bucket. Exceeding one returns `429` with `Retry-After` in whole seconds.

| Operation | Sustained | Burst |
| --- | --- | --- |
| `search` | 30/min | 10 |
| `get_note` | 120/min | 30 |
| `contribute` | 30/min | 20 |
| `update` | 30/min | 20 |
| `retire` | 10/min | 5 |
| `snapshot` | 2/hour | 1 |

`retire` is deliberately the tightest bucket: retirement is rare and
irreversible, and a loop that deletes is worse than a loop that writes.

The **pre-auth guard** is IP-keyed and charged *before* the credential is looked
up, because verifying a credential is itself a database round trip and the quota
cannot cover the cost of the check that produces its own key. It is a slowapi
`Limiter` owned by the vault — a third-party import, not a host import, so the
isolation rule is intact and this instance is independent of HSS's. Defaults to
`600/minute`, deliberately loose: it is a floor that stops anonymous hammering,
not a quota, and one egress address may legitimately carry several credentials.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VAULT_PREAUTH_RATE_LIMIT` | `600/minute` | Per-IP ceiling before authentication |
| `VAULT_RATE_LIMIT_STORAGE_URI` | `memory://` | Set to `REDIS_URL` to share across workers and dynos; falls back to in-memory if Redis is unreachable |

The guard is attached as a **router-level dependency**, not a route decorator.
FastAPI solves dependencies before calling the endpoint and authentication is a
dependency, so a decorator would charge after the round trip it exists to
prevent. Do not "simplify" it into a decorator.

`search`, `get_note` and `snapshot` match the integration spec. **`contribute`
deliberately does not.** The spec's 10/min burst 3 assumes contributions trickle
in; they arrive in batches instead — a librarian session settling nine notes, an
importer replaying a corpus of fifty — so burst 3 throttled every real run
without touching the abuse case, which is sustained rate. `update` takes the
same shape in its own bucket, so a corpus-wide backfill cannot starve new
contributions. The reasoning is on `LIMITS` in `rate_limit.py`.

Raising the burst does **not** make concurrent writes fast. The governed write
path holds a corpus-wide advisory lock, so simultaneous writes serialize on it
rather than on the limiter. `VAULT_DB_POOL_SIZE` was a second serializer until it
moved to 2; at 1 a concurrent write failed on the pool timeout instead of
queueing. The burst makes *sequential* batches fast, which is what the only
client actually does.

**The quota's buckets are per process.** Each Gunicorn worker holds its own, so
the effective ceiling is the stated limit times the worker count — two,
currently. That is a known factor on a single host. Across hosts it stops being a
limit at all, which is the point at which a shared backend becomes necessary
rather than tidier. The pre-auth guard can already take one via
`VAULT_RATE_LIMIT_STORAGE_URI`; the quota cannot, and would need the same
treatment.

## Saturation

An exhausted vault pool — every connection checked out, `pool_timeout` elapsed —
raises `sqlalchemy.exc.TimeoutError`, which the application maps to **`503` with
`Retry-After`**, not `500`. Saturation is a load condition, not a defect: a `500`
would tell the caller not to retry something purely transient, and an error
tracker to report a bug where the truth is that the vault is busy. A rise in
these is a signal to raise `VAULT_DB_POOL_SIZE`, which means revisiting the
budget above.
