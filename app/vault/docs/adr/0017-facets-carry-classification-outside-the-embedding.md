# 17. Facets carry classification, outside the embedding text

Date: 2026-08-12

## Status

Accepted

## Context

Notes need to be relatable to each other by things that are not topics: which
project a note belongs to, which area of work, which system. The operator's
request was for "project tags and the like so that notes can be related to each
other, even if they are excluded from embeddings."

The corpus is already doing this badly. `hss` (3 notes) and `b2-migration` (3
notes) are project names sitting in `tags` alongside genuine topical keywords
like `gotcha`, `unity`, and `postgres`. The need is real and currently met by
overloading the one list that was designed for something else.

`tags` is the wrong home, and not merely on tidiness grounds. **ADR 0013 puts
`tags` in the embedding text.** A shared tag therefore moves every note carrying
it closer to every other note carrying it, in the exact vector space the dedup
gate scores against.

That was measured rather than assumed. Ten real corpus documents were embedded
twice, once as assembled today and once with one shared `project/highscoreserver`
tag added:

| | untagged | with a shared tag |
| --- | --- | --- |
| max pair | 0.6249 | 0.6469 |
| mean pair | 0.2817 | 0.3202 |

Mean inflation **+0.0385**, max **+0.0825**, and **all 45 pairs rose** — not a
tail effect, a uniform shift.

Set against the calibration in ADR 0016's amendment, this is decisive. The dedup
floor (highest known-distinct pair) is 0.7406 and the ceiling (lowest known
duplicate) is 0.7500: a margin of **0.0094**. A mean inflation of 0.0385 is
**over 4x that entire margin**. Adding project tags to `tags` would lift the floor
to roughly 0.78, *above* the duplicate ceiling — converting a narrow but real
separation into an inversion, and making `flag_at` calibration not merely harder
but impossible on this model.

So classification has to live somewhere the embedding text never reads.

## Decision

### A `facets` JSONB column, excluded from the embedding by construction

`vault_documents.facets JSONB NOT NULL DEFAULT '{}'`, mapping a facet name to a
list of values: `{"project": ["highscoreserver"], "area": ["backend"]}`.

Excluded from `assemble_embedding_text` the way `frontmatter` already is — by
not being one of the fields `EmbeddableDocument` declares. The exclusion is
structural, not a filter rule: a new facet cannot leak into the embedding by
being spelled a particular way, because the assembler never sees the column.

That structural property is the reason this is a column rather than a reserved
prefix inside `tags`. The alternative considered was namespaced tags —
`project/hss` living in the existing GIN-indexed array, with
`assemble_embedding_text` stripping anything namespaced. It has a genuine
advantage this decision gives up: **one query surface**. Filtering "tagged
`unity` or in project `hss`" would be a single operator against a single index,
where it is now a union across two columns with two index types. It also keeps
the vocabulary continuous, since `hss` is already a tag today.

It was not adopted because it makes ADR 0013's guarantee conditional on string
content. A tag that happens to contain `/` and is meant topically silently
stops being embedded, and nothing fails — the note simply retrieves worse. Given
that the dedup margin is 0.0094 wide, the property "classification is never
embedded" is load-bearing enough to be enforced by the schema rather than by a
rule someone can forget.

Values are shape-validated in the database and vocabulary-validated in
application code, following ADR 0009: which projects exist is a data change, not
a migration.

Normalization must be lossless. Distinct input names such as `" project"` and
`"project"` both strip to the same stored key; accepting both would make one
assignment silently overwrite the other. Such collisions are rejected at the
shared create/update transport model and again in domain normalization for
non-HTTP callers. Values are not implicitly merged because that would conceal a
malformed request and make its digest semantics ambiguous.

### `related_ids` and `source_ids` become reachable

Both columns already exist and are already outside the embedding text. Zero rows
use either, because `VaultContributionRequest` carries only title, body, tags,
and source_url — the write path has never been able to set them.

Note-to-note linking therefore needs no schema at all, only contract. They stay
opaque `TEXT[]` of document ids: unvalidated against existence on purpose, since
a contribution may legitimately reference a note that is archived, flagged, or
not yet written, and a foreign key would make the write path fail on a
referential constraint for reasons ADR 0002 already rejected for audit events.

### Frontmatter keys are promoted into facets at import

This is the obligation a separate column creates, so it is stated rather than
implied. Project tags arrive in **markdown frontmatter**, and `frontmatter`
JSONB exists to re-emit keys the schema does not model. A key that should have
become a facet but stayed in `frontmatter` is a silent failure: the note
projects back correctly and is simply unfindable by project.

Promotion is therefore explicit and one-directional — the importer maps known
frontmatter keys to facet names, and a promoted key does **not** also remain in
`frontmatter`, or the projector would emit it twice.

## Consequences

**Search filtering must push down into both arms.** A facet filter applied after
reciprocal rank fusion returns fewer than `limit` hits and reorders what remains,
because RRF ranks over the candidate sets each arm produced. The predicate
belongs in both the lexical and vector queries.

**The vector arm must over-fetch when filtered.** pgvector's HNSW index
post-filters: it retrieves its candidate list by distance and *then* applies the
`WHERE` clause, so a restrictive facet filter can return far fewer rows than
requested — or none — while matching documents exist. Whatever builds filtered
search has to raise the candidate count and cannot assume `limit` in means
`limit` out. This is a known pgvector property, not a bug to fix here.

**Two index types.** GIN on `text[]` for `tags` (`&&`, `@>`) and GIN on `jsonb`
for `facets` (`@>`). Both are index-backed and combine under a BitmapAnd, but a
query touching both is planning against two access paths.

**A typed edge table is deferred, not rejected.** `vault_document_relations`
(`document_id`, `relation`, `target`) is the right shape once relations need
*traversal* — reverse lookups ("what references this note?"), relation types
beyond the two arrays, or integrity guarantees. The trigger to build it is the
first requirement to query relations backwards. Until then, arrays and a JSONB
column serve forward-only reads without a join, and the migration from arrays to
edges is mechanical.

**Facets are not a policy key.** ADR 0010 makes `vault_path` the only policy key
and this does not change that. A facet must never gate a read: it is authored
content, editable by a contributor, and moving a security boundary into it would
repeat the mistake ADR 0011 avoided by refusing to gate on `doc_status`.

**`hss` and `b2-migration` stay as tags until migrated deliberately.** Rewriting
them into facets changes those notes' embedding text and requires a re-embed, so
it is a data migration with a cost, not a cleanup. It is worth doing — they are
inflating dedup similarity today — but as its own operation.
