# 0045. The note listing is sorted, and its cursor is opaque

Date: 2026-09-04

## Status

**Accepted 2026-09-04. Phases 1 through 4 implemented.**

Phase 1 (PR #27): the cursor is opaque, carries the order it belongs to, and
every listing -- the default included -- pages on a compound `(key, id)`
keyset. No sort was added, which was the point of shipping it alone.

Phase 2 (PR #28): `sort=path|updated|created`, `created_at` in the brief
projection and the note summary, and migration `0020_note_listing_sort_indexes`
in the vault lineage. The index shape decided below was changed during
implementation and the decision is restated here as shipped, with what was
measured; see "Two indexes" and the note that follows it.

Phase 3: `sort=title`, ascending under the database's collation and using the
same `(key, id)` cursor shape as every other order. No title index was added;
the corpus-scale plan is recorded below.

Phase 4 (PR #30): the browse console's sort control, timestamps on listing
rows, and the folder and breadcrumb behaviour for non-path orders.

## Context

`GET /notes` (ADR 0039) orders by `vault_path` and pages by keyset. `after` is
not merely derived from a `vault_path` -- it *is* one, and the endpoint's own
OpenAPI description says so. That works as a bare cursor for exactly one reason:
`vault_path` is `UNIQUE`, which makes it a total order and a legal cursor.

Path order is the corpus's own order, and it is the right default. The browse
console's breadcrumbs and folder strip are downstream of it: a folder is a real
place to stand only because the rows arrive grouped by where they live.

What path order cannot answer is when. "What changed lately" and "what is new"
are the two questions a reader brings to a corpus that agents write faster than
a human reads, and today the only way to ask them is to export the vault and
sort it somewhere else -- the same friction ADR 0039 existed to remove.

Two facts about the data shape the decision:

`updated_at` is already in `DOCUMENT_BRIEF_COLUMNS` and in `VaultNoteSummary`,
so a recency sort needs no new projection. It is also curated rather than
incidental: `set_status` and `set_promotion_status` deliberately do not move it,
so it means "an author changed this note", not "something happened to this row".
`created_at` is on the table but in neither the brief nor the summary.

Title order is not a near-duplicate of path order, which a first reading of this
assumed. Notes live under `Agent/notes/` and wiki pages under `Agent/wiki/`
(`read_policy.py`), so at root scope path order places every wiki page after
every note. Title order interleaves them. The two orders resemble each other
only inside a single folder.

## Decision

Add sorting to `/notes`, and make the cursor opaque.

`sort` is a validated enum -- `path` (default), `updated`, `created`, `title` --
mapping to a fixed column pair. Nothing from the request reaches `ORDER BY` as
text, which is the invariant AGENTS.md states for `sort_order` and `period`.

`title` joined `NoteSort` in phase 3. It is worth having because it is *not* a
near duplicate of path order, for the reason the Context gives. It ascends
under the database's collation; the API does not promise a language-neutral or
case-folded order that PostgreSQL was not asked to provide.

The two time orders are descending, and direction is not a request parameter.
Each order has one useful direction -- a listing of the least recently updated
notes is a question nobody asks -- and offering the other would double the
cursor states to serve it. The ascending variants stay deferred, and the index
shape below leaves them free to add.

Every sort is a compound keyset on `(key, id)`. `updated_at`, `created_at` and
`title` are all non-unique, so the key alone is not a total order and a bare
cursor would skip or repeat rows. `path` takes the same shape despite
`vault_path` being unique, so there is one paging rule rather than two.

`next_cursor` becomes an opaque base64url token carrying the sort and the
compound key. `after` stops being a `vault_path` and stops being documented as
one. A token whose sort disagrees with the request is refused with 422 rather
than honoured: switching sort mid-walk is a new walk, and silently re-seating a
cursor into a different order is how a listing skips rows without saying so.

The default is unchanged. Omitting `sort` yields exactly today's order, so the
exporter -- the only other caller of the path listing, which depends on path
order inside a REPEATABLE READ transaction -- is untouched by construction.

`created_at` joins `DOCUMENT_BRIEF_COLUMNS`, `VaultNoteSummary` and
`note_summary`, so a listing row can say when it was written as well as when it
changed.

Two indexes, in a vault-lineage Alembic revision: `(updated_at, id)` and
`(created_at, id)`, both ascending. The existing
`idx_vault_documents_kind_status_updated` leads with `kind` and cannot serve a
kind-agnostic recency page.

**This is not the shape first decided here, which was `(status, updated_at
DESC, id)`.** Two corrections, both made while writing the migration:

*No leading `status`.* The listing filters `status IN ('active', 'archived')`
-- two values of three, so nearly every row. Leading with it buys almost no
selectivity while putting a non-equality ahead of the ordering key, which is
what stops the planner walking the index in order: it would gather the matching
rows and sort them, which is the cost the index exists to avoid.

*Ascending, though the listing reads newest first.* A btree scans either way,
so `(updated_at, id)` read backwards is exactly `ORDER BY updated_at DESC, id
DESC`. What cannot be reversed as a unit is a *mixed* index, so a key declared
DESC beside an ascending tiebreaker would have served the one query it was
written for and left the deferred ascending variants needing a third index.

Measured rather than reasoned: with `enable_seqscan` off, both orders plan as
`Index Scan Backward` with no Sort node, and the deferred ascending variant
plans as an Index Only Scan on the same index. Not measured is whether the
planner *chooses* them -- the corpus available locally is a single row, where a
sequential scan is correct and the planner says so.

No title index was added with phase 3. `ORDER BY title, id` needs a plain btree
on that exact pair; the existing `vault_path text_pattern_ops` index cannot
serve a different column or collation. Measured on the configured development
corpus on 2026-09-05: all 75 documents were readable, and `EXPLAIN (ANALYZE,
BUFFERS)` chose a sequential scan plus an in-memory quicksort using 38 kB, with
1.34 ms total execution. At this scale a third write-time index costs more than
the sort it would avoid. Re-measure on the deployed corpus as it grows, and add
the plain `(title, id)` btree only when that plan shows the sort is material.

Sorting is a mode of the browse console, not a console of its own. The sort
control sits in the filter row, and ordering stays orthogonal to the path
prefix, so "recently updated under `Human/03 Projects/`" is expressible. While a
non-path sort is active the folder strip is suppressed and the breadcrumb is
labelled as a filter rather than a location -- under any non-path order there
is no location, only a scope.

## Consequences

The cursor's contents stop being part of the contract. That is the point, and it
is also the breaking half: a caller holding a bare `vault_path` in `after` gets
a 422 after this ships. Cursors are page-to-page ephemera and `/notes` is recent,
so no migration window is offered; the refusal names the problem.

Adding a sort afterwards costs an enum member, a column pair and a test. The
cost of the fourth sort was paid in phase 3, in the codec rather than in a
parallel paging implementation.

Two more indexes to maintain on every write to `vault_documents`.

The browse console gains a mode in which its own navigation model does not
apply. Suppressing the folder strip is what keeps that honest; leaving it to
render a jumble of unrelated folders would be the quiet failure.

No MCP tool lists notes, so the tool surface does not change and no client or
`knowledge-vault` skill update is needed anywhere in this work.

## Alternatives considered

**A separate console for recency.** Rejected. Consoles here are split by
authority: ADR 0039 gives review and browse different credentials because
`vault:review` may only be granted to a family holding `vault:read` alone. A
recency view needs `vault:read` and `vault:propose` -- exactly the browse
console's authority -- so a third console would be a second credential for the
same powers, paying the separation tax to separate nothing. It would also
duplicate or force the extraction of `openNote`, `edgeList` and `proposeForm`,
which is most of `browse.html`. Revisit if the view grows its own vocabulary --
per-author filtering, unread state, anything with its own persistence -- at
which point it stops being an ordering and becomes a surface.

**A bare cursor plus a `sort` parameter.** Rejected. The key alone is not
unique for three of the four sorts, so rows sharing a timestamp or a title
straddle the cursor and are skipped or repeated.

**Separate `after_updated_at` and `after_id` parameters.** Rejected. It leaks
more of the query shape than the value it replaces and multiplies with every
sort added.

**OFFSET paging for the time sorts.** Rejected for the reason the endpoint
rejected it originally: insertions behind the cursor skip and repeat rows.

**Sorting by `content_revision`, as "most edited".** Rejected. Churn is not
importance, and a listing that ranks by it invites the reading that it is.

**Sorting by relevance.** That is `/search`. A listing with no query has no
relevance to rank by.

**Exposing `kind`, `doc_type` or `doc_status` as sorts.** They are filters. The
tag and facet filters already narrow on them, and as orders they would multiply
cursor states for no question anyone asked.

## Deferred

Ascending variants of the time sorts (oldest first). Sorting in the export,
which has its own ordering contract. Per-sort rate limiting: the listing bucket
is shared; title-order cost is measured before either an index or a distinct
limit is introduced.
