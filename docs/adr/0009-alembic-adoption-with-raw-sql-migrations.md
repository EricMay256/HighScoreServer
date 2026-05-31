# 9. Alembic adoption with raw-SQL migrations

Date: 2026-05-31

## Status

Accepted

## Context

Until now the schema was managed by `db/schema.sql` — a single file of
`CREATE TABLE IF NOT EXISTS` statements applied by hand (`psql -f`) locally and
piped into `heroku pg:psql` on deploy. That approach is idempotent and fine for
a greenfield database: running it against an empty DB builds everything, and
running it again is a no-op.

What it cannot express is *change to an already-deployed table*. `CREATE TABLE
IF NOT EXISTS` silently skips a table that already exists, so adding a column,
adding a constraint, or creating a new dependent table on a live database has no
safe, repeatable, ordered representation. The workflow had no notion of "apply
the changes since the last known state" — only "build from nothing" or
"hand-write a one-off `ALTER` and hope every environment ran it."

The validated-runs / cumulative-scoring feature (see `specs.md`) being applied
to a live database non-destructively (it contains actual user info now) is the
forcing function: it is the first change that *alters deployed production tables*:
adding columns to `game_modes` and `scores`, and introducing new `runs` and
`submission_idempotency` tables that reference them. Shipping that against a live
database by editing `schema.sql` and re-piping it would do nothing to the
existing tables. A migration tool that provides a versioned, ordered, reversible
record of schema deltas with a per-database notion of "current revision" is preferable
to raw sql "migration-0001.sql" files that have to be modeled, applied, and tracked.

One constraint shaped which tool and how:

- **ADR 0002 (raw SQL over ORM)** is settled. Whatever we adopt must not drag in
  an ORM or model layer, and must keep hand-written SQL the thing a reviewer
  reads.

## Decision

Adopt **Alembic** as the schema-migration tool, used as a **raw-SQL migration
runner only**:

- Migrations are hand-written DDL via `op.execute(...)`. **No SQLAlchemy ORM
  models, no autogenerate.** `migrations/env.py` sets `target_metadata = None`
  precisely so there is no model graph to diff against — autogenerate is
  structurally disabled, not merely unused.
- SQLAlchemy is present only as Alembic's engine/connection layer (it reads
  `DATABASE_URL`, normalizes Heroku's `postgres://` scheme, and opens a
  connection). It is never used to *describe* the schema.
- **Baseline `0001_baseline`** reproduces the live `public` schema exactly, with
  production's constraint/index names spelled out, verified byte-equivalent to a
  production schema-only dump. Heroku platform objects (the `_heroku` schema,
  `pg_stat_statements`, extension-management event triggers) are deliberately
  excluded — they are platform-provisioned, not application schema.
- **Stamp, don't upgrade, on pre-existing databases.** A database whose objects
  predate Alembic (production, an existing dev DB) is marked
  `alembic stamp 0001_baseline` once; only fresh/empty databases (CI, a new
  clone, a throwaway) run `alembic upgrade head` to build from scratch.
- **Deploy applies migrations in the Heroku release phase.** The `Procfile`
  carries `release: alembic upgrade head`; a failed migration aborts the release
  before the new code serves traffic.
- **Grants stay out of migrations; they live in `db/role.sql`.** Revisions carry
  structural DDL only. The restricted `leaderboard_app` role and its grants are
  applied per-environment from `db/role.sql`, which remains the source of truth
  for the role. This is a deliberate separation grounded in the deployment
  reality, not a temporary workaround (see below).

### Why grants are kept separate from migrations

A migration runs identically in every environment and, under the release phase,
is fundamentally about the **production** schema. Role grants are not like that —
they are real only where a restricted role actually exists, and on this project
that is **dev / CI / local, never production**.

The reason is the platform, and it is settled, not provisional:

- Production is a single-dyno Heroku app on an **Essential-tier** Postgres plan.
  Essential plans expose only the **default credential** — the owner role the
  app connects as. They do **not** support additional credentials, and the
  deploy user is non-superuser without `CREATEROLE` (verified 2026-05-31: the
  connecting role reports `rolsuper=f`, `rolcreaterole=f`, `rolcreatedb=f`).
- So a `leaderboard_app` role cannot be created in production at all — not by a
  `GRANT`/`CREATE ROLE` in a revision (it would raise `role "leaderboard_app"
  does not exist` / `permission denied to create role` and, under release-phase
  migrations, **abort the deploy**), and not by hand. The supported route to a
  second credential is a Standard-tier feature, and there is **no plan to
  upgrade** for it.

Putting the role into the migration stream was therefore the wrong layer: it
would force an environment-specific, production-absent concern through a pipeline
whose job is production schema, and would either fail on prod or survive only by
wrapping an object prod will never have in defensive guards. Keeping grants in
`db/role.sql` matches reality cleanly:

- **Dev** uses the least-privilege `leaderboard_app` role; `db/role.sql` is
  applied there and a missing grant fails loudly — dev is the strict gate that
  validates the grant layer.
- **Production** runs as its owner role, which holds every privilege implicitly,
  so `db/role.sql` is never executed there; it is kept accurate as documentation
  of the intended least-privilege posture, portable to any future environment
  that *can* host a restricted role.

If production ever moved off Essential to a tier that supports a real restricted
credential, revisiting this would be its own decision (and its own ADR). It is
explicitly out of scope here, and not planned.

### What this decision explicitly does not do

It does **not** adopt SQLAlchemy as an ORM, and it does **not** commit the
project to asyncpg. Schema *tooling* and the runtime *driver* are separable:
Alembic versions the schema; psycopg2 (sync) still serves requests. The async
question remains where ADR 0005 left it.

## Consequences

### Positive

- Deployed tables can now be altered safely, in order, with a per-database
  record of what has been applied. Schema change has history, not just a
  current-state snapshot.
- Release-phase migration ties schema to deploy and fails closed: a bad
  migration aborts the release instead of leaving code and schema mismatched in
  production.
- Raw-SQL migrations preserve the posture of ADR 0002. A reviewer reads the
  exact DDL that runs; there is no generated layer to audit and no model/schema
  divergence to reconcile.
- The baseline was verified byte-equivalent to production's `public` schema, so
  a fresh `alembic upgrade head` (CI, a new contributor) reproduces production
  rather than approximating it.

### Negative

- There are now two textual representations of the schema — `db/schema.sql` and
  the migration history — and only the latter is authoritative. This invites
  drift if the snapshot is mistaken for the source of truth. Mitigated by
  relabeling `db/schema.sql` with a header that marks it a non-authoritative
  bootstrap snapshot and points at Alembic.
- Alembic pulls SQLAlchemy into the dependency set for a project that
  deliberately avoids ORMs. Accepted because SQLAlchemy is confined to the
  connection/engine layer and the ORM is structurally excluded
  (`target_metadata = None`); the cost is dependency weight, not a creeping
  abstraction.
- The stamp-vs-upgrade asymmetry is an operational footgun: running `upgrade`
  against a pre-existing, unstamped database fails because the baseline objects
  already exist. The rule ("stamp existing once, upgrade only fresh") is
  documented in the README and CLAUDE.md, but it is a procedure a human must get
  right.
- The grant/migration split is non-obvious and must be taught: schema lives in
  migrations but role grants live in `db/role.sql`, applied separately, and the
  reason (production can't host the restricted role, so grants are a dev-only
  concern that doesn't belong in the production-facing migration stream) is not
  self-evident from the code. It is documented here, in `db/role.sql`'s header,
  and in CLAUDE.md.
- Downgrades that `DROP` are destructive; the baseline downgrade drops every
  application table with `CASCADE`. This is acceptable only against throwaway
  databases, and is labeled as such in the revisions — but the capability to
  destroy data with a single `alembic downgrade` against the wrong
  `DATABASE_URL` now exists.

### Neutral

- ADR 0002 (raw SQL over ORM) is unaffected: Alembic is used without ORM models
  or autogenerate, so the raw-SQL decision still holds in full.
- ADR 0005 (sync over async) is unaffected: adopting a migration framework does
  not select a runtime driver. Async remains deferred on its own triggers.
- Heroku platform objects are intentionally absent from the baseline and from
  any future revision; a fresh build constructs the application's `public`
  objects and nothing else. When diffing future dumps against production, those
  platform objects (plus `alembic_version` and dump preamble tokens) are
  expected, ignorable noise.
- `DATABASE_URL` is read from the environment by `migrations/env.py`, which loads
  `.env` with `override=False` — so a URL exported in the shell (a prod stamp, a
  throwaway target) wins over `.env`, keeping deliberate operations from
  accidentally hitting the dev database.
