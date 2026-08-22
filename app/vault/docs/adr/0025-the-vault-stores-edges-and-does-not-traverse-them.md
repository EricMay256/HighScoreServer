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

Two vocabularies exist. Today they do not meet; the decision below translates between them.

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

### The database is uniformly id-based; each tree keeps its own idiom

Neither vocabulary wins, and neither is abandoned. They are translated at the two boundaries
that already exist, so that **inside the database every edge is an id**, while a human writing
markdown still writes `[[Some Note]]` and never sees a uuid.

**On import, `Human/` wikilinks resolve to ids.** `Parent`, `DependsOn`, and `SeeAlso` all
land in `related_ids`, and the original typed frontmatter is preserved verbatim in
`frontmatter` JSONB — so the distinction between "depends on" and "see also" is not destroyed,
it is simply not indexed separately. If a typed query is ever needed, the frontmatter is where
it already lives. Resolution happens once, at import, which converts a fragile *name*
reference into a stable *id* reference.

A wikilink that resolves to nothing is dropped from `related_ids` rather than stored — an
unresolved name is not an id, and `related_ids` holds ids. Nothing is lost, because the
unresolved link is still in `frontmatter` exactly as written.

**On export, agent `related_ids` render as wikilinks.** The exporter holds every id-to-slug
pair for the run before it writes anything, so it emits `SeeAlso: ["[[slug]]"]` alongside the
engine-owned `RelatedIDs`. That is not duplication: the Metadata Standard makes `SeeAlso` a
universal `List<WikiLink>` for readers and `RelatedIDs` Agent Note plumbing for the engine.
Without it, an exported relation is a uuid that Obsidian cannot follow, so the graph a human
opens the vault to browse is invisible in both directions.

Ids that do not resolve within the run are omitted from `SeeAlso`, and so are ids pointing
outside the exported prefixes. A broken wikilink is worse than an absent one, and dangling
edges are legal here (below).

**The prerequisite, which ADR 0012 already named.** A human note brings no identity of its
own, so its id is manufactured and "stable only as long as the row survives — a rename-plus-edit
will break references to it." The bridge inherits that limitation rather than creating it, and
it becomes more visible once edges point *at* human notes. The available fix is the one Stage A
made for agent notes: assign an `ID` on first import and write it into the file, turning an
identity-less source into one with durable identity. That is a decision for the human importer,
which does not exist yet, and it should be made before the first inbound edge does.

### There is no reverse index, and here is when to add one

`related_ids` and `source_ids` carry no GIN index, so "what points at this note" is a
sequential scan. Measured against production on 2026-08-22:

```
notes with related_ids : 3        total edges : 7
backlink query         : Seq Scan, 70 rows removed, 0.036 ms
```

An index optimising that would be maintained on every `contribute` and `update` — the hot
paths — to save microseconds on a query nothing runs. So: not yet.

**The signal to watch is document count, not edge count.** A sequential scan reads every row
whether or not it has edges, so the cost grows with the corpus while the benefit stays flat.
Re-measure and add the index when any of these becomes true:

- a backlink lookup enters a request path rather than being a one-off — anything issuing one
  per result, or per hop of a client-side walk, multiplies the scan by the fan-out;
- the same `EXPLAIN ANALYZE` above crosses roughly a millisecond, which on this shape means
  tens of thousands of documents;
- compilation starts asking "which pages cite this note", which is the reverse of `source_ids`
  and the first genuinely recurring reverse query the roadmap contains.

When that happens the change is small and self-contained: `CREATE INDEX ... USING gin
(related_ids)`, and `source_ids` separately if the compile query is the trigger. `tags` already
carries exactly this shape of index, so it introduces no new concept. Index the arrays rather
than building an edge table — a table would be a second source of truth for a fact the arrays
already hold and the wire contract already exposes, and one carrying foreign keys could not
hold the dangling edges this ADR deliberately permits.

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
