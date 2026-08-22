# 25. The vault stores edges and does not traverse them

Date: 2026-08-22

## Status

Proposed.

Names a structure that eight earlier ADRs use without defining. Depends on ADR 0008
(fetch-by-id resolves archived, withholds flagged), ADR 0014 (`ai_read` excludes at import and
at query), and ADR 0017 (facets classify, outside the embedding). Constrains ADR 0023's
promotion lineage and any future compilation work.

## Context

The corpus has a relationship graph. Nothing says so.

`related_ids` and `source_ids` appear in ADRs 0008, 0012, 0014, 0016, 0017, 0018, 0019 and
0023 — always incidentally, as things that survive a retirement, that carry no foreign key,
that become reachable, that cost no embedding call to change. Every one of those statements is
about an edge, and none of them says what an edge *is*, which ones exist, which direction they
point, or who is allowed to walk one.

That gap is cheap to live with while nothing traverses, and expensive the moment something
does — because traversal is where the read policy, the dangling references, and the fan-out
all arrive at once.

### What is actually an edge

Two vocabularies exist, and they do not currently meet.

**Id-based, in the database, used by the service:**

| Field | Direction | Enforced |
| ----- | --------- | -------- |
| `source_ids` | directed: this note derives from those | no FK |
| `related_ids` | stated one way, symmetric in intent | no FK |
| `compile_run_id` | directed: this wiki page came from that run | **FK**, `ON DELETE RESTRICT` |
| `vault_review_cases.candidate_document_id` | directed: this case judges that note | **FK**, nullable |
| `vault_review_cases.similar_documents` | evidence: this judgement weighed those | JSON, no FK |

**Wikilink-based, in markdown, used by Obsidian and humans:** `Parent`, `DependsOn`, `SeeAlso`
from the Metadata Standard, plus `Related` on a Wiki Page. These resolve by *title*, land in
`frontmatter` JSONB on import, and are re-emitted verbatim by the export.

**Not edges, and worth saying so:** `facets` is set membership, an axis notes share rather
than a pointwise relation (ADR 0017); `tags` describe; `aliases` are other names for the same
node; and embedding similarity is computed on demand, never stored, and therefore not part of
this graph at all.

## Decision

**The vault stores edges. It does not walk them. Traversal belongs to the caller, one
policy-checked fetch per hop.**

This is what the code already does — `related_ids` and `source_ids` are persisted and
returned and never resolved — and it is recorded here as a decision rather than left as an
absence, because the alternative is attractive and worse.

### Why server-side traversal is the dangerous version

An `expand` or `depth=2` parameter looks like an obvious convenience. It would break three
things at once:

- **The read policy is enforced per fetch, not per graph.** Both surfaces pass
  `readable_only=True` on fetch-by-id, so one hop is checked against `READABLE_PATH_PREFIXES`
  and `READABLE_STATUSES`. A server-side walk that loaded neighbours through the repository
  would bypass that unless every hop re-applied it — and `get_by_id` defaults `readable_only`
  **off**, deliberately, so that review and reconciliation can load what the public surface
  hides. The safe default and the traversal default point in opposite directions.
- **Edges are unvalidated by design**, so a walk meets dangling ids as a matter of course and
  has to decide what that means. A caller doing it hop by hop just gets a 404 and moves on.
- **Fan-out is unbounded.** Fifty `related_ids` per note at depth two is a fetch storm against
  a quota sized for interactive use, and cycles are legal because nothing forbids them.

Keeping traversal client-side makes each of those a non-event: every hop is an ordinary
authenticated fetch, subject to the same policy, quota, and 404 as any other.

### Edges stay opaque and unvalidated

No foreign keys on `related_ids` or `source_ids`, for the reason ADR 0002 gives about audit
events: a contribution may legitimately reference a note that is archived, flagged, retired,
or not yet written. A dangling edge is **normal**, not corruption, and nothing should
"repair" one by deleting it.

The two FKs that do exist are deliberate exceptions, and both are about provenance a
judgement depends on rather than association: compile provenance must not silently vanish
(ADR 0019), and a review case must not outlive its subject unnoticed (ADR 0023's amendment
made that pointer nullable rather than removing the constraint).

### The two vocabularies stay separate

Wikilinks are not rewritten into ids, and ids are not rendered as wikilinks. A `[[Some Note]]`
in a Human note means what Obsidian says it means; a uuid in `source_ids` means what the
service says. Unifying them would require the service to resolve titles — which are neither
unique nor stable — and would make every retitle a graph mutation.

They meet in exactly one place, and only by convention: a promoted Human note carries
`SourceIDs` naming the agent notes it came from (ADR 0023), which is an id-based edge written
into markdown. That is the bridge, and it is one-directional on purpose.

### There is no reverse index, and backlinks are not a supported query

`related_ids` and `source_ids` carry no GIN index, so "what points at this note" is a
sequential scan. That is acceptable at the current corpus size and is the reason backlinks are
not offered as a surface: an unindexed reverse lookup that works at seventy documents and
degrades silently is worse than one that does not exist.

Adding it is a real option later — `related_ids @> ARRAY[id]` with a GIN index answers it —
but it is a schema change with an index to maintain, and it should be made because something
needs backlinks rather than because the graph looks incomplete without them.

## Consequences

### What a client may assume

An id in `related_ids` or `source_ids` is a *claim* that a note exists, not a guarantee. Fetch
it; handle the 404. An id that resolves may still be `archived`, which fetch-by-id serves
deliberately so a reference does not dead-end (ADR 0008), and never `flagged`, which it
withholds.

### What compilation inherits

A wiki page's `source_ids` is the one place the graph is load-bearing today: provenance from a
page back to the notes it synthesized. Compilation must write those ids from service note ids
rather than filenames, and the FK on `compile_run_id` means a run cannot be deleted out from
under the pages it produced.

### What this leaves open

Whether the service ever offers a bounded, policy-checked traversal — a `neighbours` endpoint
returning one hop with the same filters fetch-by-id applies. That is a coherent middle ground
between here and a graph API, and it is a smaller decision than it looks because the policy
question is already answered: any such surface applies `readable_only=True` and
`READABLE_STATUSES` at every hop, or it is not shippable.
