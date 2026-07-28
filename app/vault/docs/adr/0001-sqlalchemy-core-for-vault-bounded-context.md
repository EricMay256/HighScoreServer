# 1. SQLAlchemy Core for the vault bounded context

Date: 2026-07-25

## Status

Accepted

## Context

ADR 0002 chose raw SQL over an ORM for the leaderboard. That remains a good fit for its
stable, PostgreSQL-specific window queries and upserts. The new knowledge-vault workload is
an inflection point rather than a reason to rewrite working leaderboard persistence. It adds
a larger persistence surface, reusable filters and projections, citation-specific result
shapes, idempotency and review records, and transactional governed writes.

The public Pydantic request/response models are deliberately not database records. The vault
needs internal records that can carry business-relevant persisted data without exposing every
column to callers. It also needs one place to own query construction and row mapping instead
of placing SQL in routes.

SQLAlchemy 2.x is already installed as Alembic's engine layer, and its psycopg dialect works
with the project's selected PostgreSQL driver. Core provides table/column expressions,
composable statements, async transaction ownership, and explicit result mappings without
introducing ORM identity maps, sessions, lazy loading, or object relationships.

The vault may initially share the existing Heroku Postgres add-on, but its migration and
connection design must not prevent moving it to a second database later. The public HSS
repository must never contain vault content or credentials.

Likewise, placing the first implementation under `app/vault/` must not permanently turn the
public leaderboard repository into the owner of an unrelated knowledge product. The package
is a staging boundary that must remain extractable into a private runtime and composable at
deployment time.

## Decision

Use SQLAlchemy 2.x Core over `postgresql+psycopg` for the new `app/vault/` bounded context.
Do not introduce SQLAlchemy ORM sessions, declarative mappings, or mapped relationships.

Separate the vault into Pydantic API models, purpose-built domain records, Core table
definitions, repositories, application services, and thin HTTP/MCP adapters. Services own
transactions and pass one `AsyncConnection` to repositories. Embedding network calls happen
before a governed-write transaction opens. PostgreSQL-specific bound SQL remains acceptable
where Core would make the operation less legible, particularly the hybrid RRF query.

Alembic remains the only production schema migration mechanism. Vault migrations use a
dedicated lineage and explicit DDL for pgvector, generated `tsvector` columns, operator
classes, and partial indexes. Core metadata supports queries and drift tests; the application
never calls `MetaData.create_all()` to migrate a deployed database.

Use a dedicated `vault` PostgreSQL schema and schema-qualified table definitions in both
deployment modes:

- By default, `VAULT_DATABASE_URL` falls back to `DATABASE_URL`, colocating the `vault`
  schema and the existing leaderboard `public` schema in one Postgres database.
- When `VAULT_DATABASE_URL` is set, the same vault engine and migration lineage target a
  second Postgres database. No vault table may have a foreign key to a leaderboard table, so
  this remains a configuration and data-migration change rather than an application rewrite.

The vault has one process-wide `AsyncEngine` per worker with an explicit pool size and
`max_overflow=0`. During the initial coexistence period, existing HSS modules retain their
psycopg async pool. Before deployment, calculate maximum connections across both Gunicorn
workers, both pools, release migrations, and operator reserve. At least 25% of the database
connection limit must remain unallocated.

Existing HSS persistence is reconsidered for incremental Core adoption only when one of
these triggers occurs:

1. Pool/process plans would consume over 75% of available connections, checkout latency
   becomes material, or another worker/process is added.
2. One transaction must atomically update vault and leaderboard records. The involved
   legacy repository moves to the Core engine first; transactions never span two pools.
3. A legacy persistence module is already undergoing material feature work and adopting
   Core there does not turn it into a system-wide rewrite.
4. Dynamic query composition is needed, equivalent SQL/row mapping appears in at least
   three call sites, or recurring mapping/parameter defects demonstrate maintenance cost.
5. Shared transaction fixtures or repository tests would remove meaningful duplicated test
   infrastructure.

## Consequences

The new vault code gets explicit layering and composable query metadata without obscuring
its PostgreSQL-specific behavior behind an ORM. API responses can deliberately expose
subsets of domain data, while vectors, generated search columns, credentials, and internal
review details remain persistence concerns.

For the first deployment, one add-on is cheaper and simpler while schema qualification,
a separate Alembic lineage, and the absence of cross-context foreign keys preserve a clean
exit. Colocation still couples backup/restore, capacity, and database-level outages. A
separate database provides independent lifecycle and stronger operational isolation at the
cost of another add-on, another connection budget, and multi-database operations.

Two pools temporarily coexist in each worker. That is operationally acceptable only with a
documented connection budget and metrics for pool utilization and checkout latency. If the
budget fails, connection ownership must be unified before vault deployment.

ADR 0002 remains accepted for existing leaderboard persistence. Core adoption there is
incremental and trigger-driven. This decision does not authorize ORM adoption, automatic
vault merges, or any repository of private vault data inside HSS.

The Core tables, repositories, services, settings, and Alembic lineage must avoid leaderboard
domain imports and cross-schema dependencies. That discipline supports both later database
separation and later repository extraction without changing the persistence model.
