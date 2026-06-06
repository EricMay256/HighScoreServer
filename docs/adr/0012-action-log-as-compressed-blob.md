# 11. Action log as a compressed blob over normalized rows

Date: 2026-05-31

## Status

Accepted

## Context

A validated run carries an *action log* — the ordered sequence of events the
server needs to recompute or replay the run. A single run can contain thousands
of actions (the transport bound is 50,000 elements). Two storage shapes were
available:

1. **Normalized** — a `run_actions` table, one row per action, linked to the
   run. SQL-queryable at the per-action level.
2. **Single blob** — the whole action log serialized to JSON, gzipped, and
   stored in a `BYTEA` column (`runs.actions`).

The deployment is a single-dyno Heroku app on an **Essential-tier** Postgres
plan, which has a hard row/storage ceiling. A normalized table multiplies row
count by the action count: a few thousand runs at thousands of actions each is
millions of rows, with the attendant index, autovacuum, and storage cost — on
the cheapest tier.

Crucially, the action log is **opaque at the API boundary** and is only ever
consumed *wholesale* by a validator selected on `scenario_version`. There is no
use case for querying or aggregating individual actions in SQL.

## Decision

Store the action log as a **single gzipped-JSON blob** in `runs.actions BYTEA`.

- The `/runs` endpoint `gzip.compress(json.dumps(actions))` on write; the
  validator decompresses and parses the whole log.
- The blob is treated as opaque: the API validates only **transport bounds**
  (it is a JSON array, element count ≤ a cap; decompressed-size enforcement at
  the endpoint), never the internal action shape.
- The scalars a query *does* need — `claimed_score`, `canonical_score`,
  `validation_tier`, `status` — are stored as columns on `runs`, not extracted
  from the blob.

A normalized `run_actions` table is explicitly **not** built. It would be the
right call the day per-action SQL analytics is genuinely needed; that day has
not come, and the spec defers it.

## Consequences

### Positive

- Row growth stays bounded to one row per run, not one per action — material on
  an Essential-tier plan.
- The opaque blob is *why* the action shape can stay deferred per scenario (see
  the deferred typed-action-shape decision): a new scenario is a new
  `scenario_version` + a new parser, with no schema change and no migration.
  Old runs keep their blob and their old parser.
- gzip compresses repetitive action logs well, further reducing storage.

### Negative

- No SQL-level per-action querying or analytics — any per-action inspection
  requires fetching the row, decompressing, and parsing in application code. The
  blob is a black box to the database.
- The blob is not indexable or constraint-checkable beyond the transport bounds
  the API enforces; correctness of the log's *contents* is entirely the
  validator's responsibility.

### Neutral

- Accepted reversal trigger: if per-action SQL analytics (anti-cheat heuristics,
  aggregate event stats) becomes a real need, add a normalized projection
  derived from the blob rather than replacing it — the blob remains the
  system of record.
- Transport bounds live in the Pydantic model and the endpoint, not the DB, so
  the size guard is enforced before the blob is ever written.
