# 0045. The note listing is sorted, and its cursor is opaque

Date: 2026-09-04

## Status

**Accepted 2026-09-04.** Nothing is implemented. The cursor codec, the
`created_at` projection, the Alembic index revision and the console control all
remain to be built; the plan runs in four phases, the first of which changes the
cursor without adding a sort.

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

Two indexes, in a vault-lineage Alembic revision: `(status, updated_at DESC,
id)` and `(status, created_at DESC, id)`. The existing
`idx_vault_documents_kind_status_updated` leads with `kind` and cannot serve a
kind-agnostic recency page.

Sorting is a mode of the browse console, not a console of its own. The sort
control sits in the filter row, and ordering stays orthogonal to the path
prefix, so "recently updated under `Human/03 Projects/`" is expressible. While a
non-path sort is active the folder strip is suppressed and the breadcrumb is
labelled as a filter rather than a location -- under a time order there is no
location, only a scope.

## Consequences

The cursor's contents stop being part of the contract. That is the point, and it
is also the breaking half: a caller holding a bare `vault_path` in `after` gets
a 422 after this ships. Cursors are page-to-page ephemera and `/notes` is recent,
so no migration window is offered; the refusal names the problem.

Adding a sort afterwards costs an enum member, a column pair and a test. The
cost of the fourth sort is paid here, in the codec, not in each one.

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
is shared and no sort is more expensive than another once indexed.
