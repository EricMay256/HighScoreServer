# 25. The vault stores edges and does not traverse them

Date: 2026-08-22

## Status

Accepted 2026-08-22.

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

### Planned and deferred: batch fetch, not `neighbours`

Client-side traversal costs a round trip per hop, and that is the only real complaint against
it. Two shapes would fix it, and they are not equally good.

**A `neighbours` endpoint** — give it an id, get its one-hop neighbourhood — is the obvious
one, and it has a problem worth naming: it is a **quota multiplier**. One request charges one
unit and performs N lookups, so the correspondence between what a caller spends and what the
server does breaks, and it breaks in favour of the caller. `LIMITS` is sized on the assumption
that a request is a bounded amount of work. It also re-creates the traversal surface this ADR
argues against, bounded at first — and `depth=2` is the obvious next request once `depth=1`
exists.

**Batch fetch by id** — `GET /notes?ids=a,b,c` — solves the same problem without either
defect. The client reads `related_ids` itself and asks for exactly what it wants, so the vault
still never walks the graph; the work is bounded by construction because the caller enumerates
it; the read policy applies per id exactly as fetch-by-id already does; and quota can charge
per id rather than per call, keeping cost and charge aligned.

The one thing `neighbours` offers that batch does not is saving a caller that has not already
fetched the note. In practice it has — that is where the ids came from.

**So: batch fetch is the planned surface, and it is deferred until something needs it.**
Whichever ships, the policy constraint is settled and not negotiable: `readable_only=True` and
`READABLE_STATUSES` apply to every id, individually, exactly as they do on a single fetch.

### What this leaves open

Whether a typed reverse query is ever wanted — "what depends on this" rather than "what
mentions this" — which the import deliberately flattens into `related_ids` while preserving the
distinction in `frontmatter`. Answering it means reading JSONB rather than an array, and it
should wait for a caller that needs the type rather than the edge.

## Amendment, 2026-08-26 — the two boundaries are built, and a Wiki Page's `Related` is one of them

The decision above describes both translations in the present tense. Neither
existed. What the code did instead was worse than not translating, because it
round-tripped: `scripts/import_vault_wiki` passed each page's `Related`
frontmatter into `related_ids` verbatim, and `export._wiki_frontmatter` wrote
`related_ids` back out verbatim, so twenty-one `[[Title]]` strings sat in a
column that holds ids and looked correct from either end. Nothing objected —
`related_ids` is deliberately not existence-checked, `remap_vault_reference_ids`
deliberately excludes non-id values from its dangling-reference report, and the
exported file matched the imported one byte for byte.

`app/vault/wikilinks.py` is now the one translation both boundaries use.
`scripts/resolve_vault_wikilinks.py` repairs rows already written the wrong way,
dry-run first like its sibling.

### `Related` on a Wiki Page is a wikilink key, so it gets the `SeeAlso` treatment under its own name

The decision above names `SeeAlso` and stops, because it was written from the
Agent Note side. The governance schema settles the wiki side the same way:
`global.yml` lists `Related` under `known_extra_keys` with `SeeAlso` as its
canonical equivalent, `SeeAlso` is a universal `list_wikilink`, and `Related` is
conspicuously **absent from `engine_owned_properties`** — where `RelatedIDs`,
`SourceIDs`, `CompileRunID` and the rest of the plumbing all appear. So a page's
`Related` was never an id list. `types.yml` recommends it for the `Wiki Page`
type, so the export keeps the name and changes the value: an Agent Note carries
`RelatedIDs` **and** `SeeAlso`, a Wiki Page carries `Related`, and all three are
projections of the same id column.

### Links are `[[slug]]`, and the title form never worked

`vault_path`'s leaf is the title's slug (ADR 0022's amendment) and Obsidian
resolves `[[x]]` against a file name, so `[[Operating the Agent Knowledge Vault]]`
pointed at a file that does not exist — the page is
`operating-the-agent-knowledge-vault.md`. Every one of the twenty-one stored
links was an unresolved link in the tree as well as a non-id in the column. The
repair therefore changes those files once, from the title form to the slug form,
and that diff is the links starting to work rather than the export churning.

### The drop is only lossless if something else holds the name

The rule above — an unresolvable wikilink is dropped rather than stored — rests
on "nothing is lost, because the unresolved link is still in `frontmatter`
exactly as written". That premise was false for exactly the rows that needed it:
`import_vault_wiki` wrote no `frontmatter` at all. The import now preserves the
original `Related` list there, and the repair script backfills it before
rewriting a column, so the premise is made true rather than assumed. `Related`
and `RelatedIDs` are both assigned keys in the exporter, so the preserved copy is
evidence in the database and is never re-emitted beside the rendered one.

### Ambiguity is reported, never resolved

A name is not a key. Two documents may legitimately share a title — the dedup
gate scores meaning, not titles — so a link naming more than one document is
handed back rather than pointed at whichever row sorted first, which would read
as a working citation while naming the wrong note. The import refuses on
ambiguity, matching what it already does with an ambiguous `SourceIDs`; the
repair script leaves the value alone, reports it, and fixes everything else.

### What this leaves open

**Settled 2026-08-26 by ADR 0030:** the write path now refuses a value carrying
whitespace or a bracket -- a name rather than an id -- while still never checking
existence. The question as it stood is kept below, because the distinction it
draws is the one that had been missing.

Whether the **write path** should check that a `related_ids` value is *shaped*
like an id, which is a different question from whether it exists and is not
answered by this ADR either way. Shape and existence are separate: the reason
edges carry no foreign key is that a contribution may reference a note that is
archived, flagged, or not yet written, and none of that licenses storing a value
that is not an id at all. Deciding it needs its own ADR, because a shape rule is
a wire-contract change on a shipped API — and because the pre-Alembic corpus
proves the id shape has not been uniform across generations.

### The repair runs before the next export, and the exporter says so

Because the export omits an unresolvable value, running it against un-repaired
rows empties the `Related` block of all thirteen pages rather than leaving it
wrong. That is the correct rendering of a column holding names — the fix is the
ordering, not a tolerance for names in the exporter, which would re-legitimise
exactly the data this amendment removes. `export._warnings` names every row
still holding a wikilink, so an operator who reaches for the exporter first gets
a per-file warning in the report instead of a silent regression in their vault.
