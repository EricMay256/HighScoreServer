# 34. Embeddings are document-level, and chunking is deferred against a measured trigger

Date: 2026-08-26

## Status

Accepted 2026-08-26. Records as a decision what ADR 0003 and ADR 0013 have
implied since the embedding table was built, and sets the conditions under
which it should be reopened. Constrains any future retrieval work; changes
nothing that ships today.

## Context

An external assessment of the vault's MCP efficiency proposed adding chunk-level
embeddings for long documents, and observed — correctly, from the outside — that
the service appears to hold one embedding per document rather than
independently addressable chunks. Its evidence was behavioural: search returns
one hit per `note_id` with no chunk identifier, heading path, or text offsets;
long wiki pages arrive as single results; deduplication is calibrated between
whole notes. It asked for the schema and the embedding assembler to be confirmed
in code before any migration was planned.

They were. The finding is stronger than the behaviour suggested.

### One vector per document is enforced, not merely conventional

`assemble_embedding_text` takes one document and returns one string — title,
aliases, tags, summary, then the entire body, unsplit. Every write path calls it
exactly once per document (`service.py`, four call sites) and embeds the single
string it returns. `VaultDocumentEmbeddingRepository.upsert` writes one row and
resolves a conflict on `(document_id, profile_id)` by **replacing** the vector.

That primary key is the whole answer. A document cannot hold two vectors under
one profile, so chunking is not something the codebase has neglected to do — it
is something the schema currently forbids. Adding it is a migration, and it was
always going to be.

The dimension is fixed the same way: `embedding` is `vector(1536)`, a width HNSW
requires, which ADR 0005 already records as a hard filter on candidate models
rather than a preference. Per-row `dimensions` provenance would be redundant
while one width is storable at all.

### What the corpus actually looks like

Measured 2026-08-26 with `scripts/measure_chunk_eligibility.py`, over 75 active
readable documents:

| | n | median | p75 | p90 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Note, estimated tokens | 61 | 377 | 533 | 855 | 1,172 |
| Wiki, estimated tokens | 14 | 1,200 | 1,705 | 1,732 | 1,814 |
| Note, headings | 61 | **0** | 0 | 3 | 5 |
| Wiki, headings | 14 | 4 | 5 | 5 | 6 |

Two populations, and they do not overlap in the way that matters. **The median
note has no headings at all** — it is one undivided argument, which is what an
Agent Note is supposed to be. The median wiki page has four, because a page is a
synthesis of several notes and its sections are those notes' subjects.

Eligibility, counting a document as chunkable only when it exceeds the threshold
*and* has at least two sections of 400 characters or more:

| Threshold | Eligible notes | Eligible pages | Added chunk vectors |
| ---: | ---: | ---: | ---: |
| > 800 tokens | 6 of 61 (10%) | 10 of 14 (71%) | +73 → 1.97× total |
| > 1,200 tokens | — | 7 of 14 | +36 → 1.48× total |
| > 2,000 tokens | 0 | 0 | 0 |

**No document in the corpus exceeds 2,000 tokens.** The assessment's own primary
threshold selects nothing. Its secondary threshold of 800 selects 21% of the
corpus and would nearly double the vector count to do it.

### The two problems chunking solves, and whether we have them

Chunking earns its complexity by fixing one of two things.

**Exceeding the model's context.** `text-embedding-3-small` accepts 8,191
tokens. The largest document in the corpus is about 1,814 — roughly 22% of the
limit. Nothing is being truncated, and nothing is close to it.

There is a real cliff further out, and it is worth naming precisely because it
is not where anyone would look for it: `VaultDocumentContentRequest.body` admits
100,000 characters, which is about 25,000 tokens, comfortably past the provider's
limit. A body between roughly 32,000 and 100,000 characters therefore passes
transport validation and is then refused by the embedding call. That refusal is
clean — `EmbeddingInputTooLong` becomes a 422 naming the reason — so this is a
capability limit rather than a defect. It is 4.4× longer than anything yet
written.

**Dilution of a whole-document vector by unrelated sections.** Plausible for the
14 wiki pages, and entirely unmeasured. No labelled query set exists against
which chunked and unchunked retrieval could be compared, so adopting chunking
now would be adopting it on the strength of the argument rather than the
evidence — and the argument is about a fifth of the corpus.

## Decision

**Every document has exactly one embedding, covering its whole text. Chunk-level
embeddings are not built now.**

When they are built, they are **additive and selective**, never a replacement:

1. The document-level vector stays for every document, permanently. It is what
   deduplication scores against, and chunk similarity is a different
   distribution that would need separate calibration before it could be
   substituted. A short contribution matching one passage of a long synthesis is
   not a duplicate of that synthesis.
2. Only documents that are both long and genuinely multi-sectioned become
   eligible. A long single argument is not improved by cutting it at an offset.
3. Chunk hits collapse to note-level results before they reach a caller, so one
   document cannot flood a page with its own passages.
4. Deduplication stays note-level until a separate chunk-aware experiment shows
   otherwise.

### The trigger to reopen this

Deliberately measurable, so that reopening is an observation rather than a mood.
Re-run `scripts/measure_chunk_eligibility.py` and reconsider when **any** of:

- a document exceeds **2,000 estimated tokens** — today the maximum is 1,814,
  and nothing at all clears this bar;
- **more than a quarter of wiki pages exceed 3,000 tokens**, approaching the
  point where a single vector genuinely cannot represent a page;
- a contribution is refused with `EmbeddingInputTooLong` in production, which
  turns the cliff above from theoretical into observed;
- a labelled query set demonstrates that whole-page vectors lose narrow
  section-level questions that chunk vectors would find.

The last one is the only one that justifies chunking on retrieval *quality*
rather than on size, and it is the one that needs building rather than waiting
for. Until such a set exists, "chunking would improve recall" is a hypothesis.

### What to design now, because it is cheap now

Nothing in the schema changes. But when chunking arrives, a chunk must not be
identified as `note-id:chunk-7` — inserting an early section renumbers every
later chunk and invalidates identifiers that nothing else changed. The stable
identity is the document id plus a normalized heading path plus a content hash,
with the human-readable locator kept separate from the hash that decides whether
a vector can be reused.

`embedded_text_sha256` already does that job at document level, and does it
better than the `content_revision` the assessment proposed: `content_revision`
increments on edits that never touch the embedded text — facets, `related_ids`,
`source_url` — so it would report staleness that is not there. A hash of the
exact text that was embedded is the precise question, and an offline reader can
recompute it from the exported frontmatter and body.

## Consequences

Retrieval keeps one vector per document, so a narrow question about one section
of a long wiki page is answered by whole-page similarity, which is weaker than
section similarity would be. That is the cost, it applies to 14 documents, and
it is accepted on the evidence above rather than overlooked.

`scripts/measure_chunk_eligibility.py` is the durable half of this decision. The
numbers in this ADR are a snapshot of 2026-08-26; the script is how the trigger
gets evaluated later, and it needs no API key and costs nothing to run.

Provenance is adequate for a portable export today — `profile_id`,
`embedded_at`, and `embedded_text_sha256` per row, with dimensions fixed by the
schema — and gains `chunker_version` only when there is a chunker to version.
One field the assessment asked for is genuinely absent: whether vectors are
unit-normalized is a property of the provider, recorded nowhere, and
`scripts/measure_dedup_similarity.py` already computes full cosine rather than a
dot product precisely because it cannot assume. An export format should state it
explicitly rather than inherit the assumption.

Nothing here blocks the search-compaction work; a compact hit already carries no
vector-level detail, and it would carry a chunk locator rather than a chunk body
if chunks ever existed.
