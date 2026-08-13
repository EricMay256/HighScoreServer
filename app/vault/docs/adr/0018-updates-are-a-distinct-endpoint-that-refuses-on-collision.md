# 18. Updates are a distinct endpoint that refuses on collision

Date: 2026-08-13

## Status

Accepted.

## Context

`POST /api/v1/vault/contributions` mints identity: every call creates a document.
There has been no way to change one. A replay returns the stored response and a
digest mismatch refuses (ADR 0016, amended 2026-08-13), so neither carries new
field values onto an existing row.

That became load-bearing when `5bdd5ad` added `summary`, `aliases`, `facets`,
`related_ids` and `source_ids` to the write contract. The 48 imported documents
predate all five and hold empty values for them. No amount of widening the
importer's payload reaches them: the keys are in the ledger, so every note
replays. The corpus can only ever gain those fields through some operation that
does not exist yet.

Two shapes were considered.

**Let a version-mismatched replay overwrite.** Tempting, because the rows that
need updating are exactly the rows whose digests are unverifiable, so it looks
free. It is not. Retry-after-timeout is the canonical reason idempotency keys
exist: a client that never learned an outcome retries, sometimes from a queue,
sometimes carrying a payload older than the one that landed. Under "newer wins"
that retry silently replaces current content with stale content, and
`vault_documents` keeps no history to recover from. It would also bypass the
dedup gate — or run it against the row being updated, which scores 1.0 against
itself — and force a re-embed on a path documented as buying neither an
embedding call nor a write.

**A distinct endpoint.** Overwrite becomes the caller's stated intent rather than
a consequence of a network failure, and no client can trigger it by accident.

## Decision

**`PUT /api/v1/vault/notes/{note_id}` replaces one document's caller-supplied
content, runs the same dedup gate excluding itself, and refuses on collision.**

### A replacement, not a patch

The body states what the document should now be. A patch would need a way to
express "leave this alone" that is distinct from "set this to empty", and every
optional field would carry that ambiguity; a caller that sends `facets` and omits
`tags` would be asking an unanswerable question. A replacement has one reading:
an omitted list is an empty list.

The cost is that a caller changing one facet resends the body. For the only
client that exists — a projector holding the whole note already — that is free.

### No idempotency key

A full replacement is idempotent by construction: send it twice and the second
converges on the same state. The contribution path needs a key because it mints
identity and must not mint it twice; there is no second identity to mint here.

### The same dedup gate, excluding the document being updated

An update can create a duplicate exactly as a contribution can — edit note A
into a copy of note B — so a surface that skipped the gate would be the easy way
around the one guarantee the vault exists to enforce.

`find_similar` therefore gains `exclude_document_id`. Without it a candidate
scores 1.0 against its own stored vector and every edit, including a no-op,
looks like a duplicate. The exclusion is a predicate HNSW applies after its scan,
so the query over-fetches by one and trims — the same over-fetch rule hybrid
retrieval already needs.

### A collision refuses; it does not flag

This is where the update path deliberately diverges from ADR 0016. A contribution
that flags is still **written**, because the review queue needs the content in
order to adjudicate it, and the document did not exist before, so a flagged row
is strictly more than there was.

An update that flagged would set an existing, active, readable document to
`Flagged` — removing it from the read surface (ADR 0008) as a side effect of an
edit. The caller was trying to improve the document and would have made it
disappear. Refusing leaves the row byte-for-byte as it was and returns 409 naming
what it collided with, which the caller can act on.

### Embedding is conditional

`vault_document_embeddings.embedded_text_sha256` already exists to answer "did
the text that produced this vector change" (ADR 0013). The update assembles the
embedding text, hashes it, and embeds only on a difference. An edit touching only
`facets`, `related_ids` or `source_url` costs no embedding call — which is
precisely the shape a facet backfill has, so the backfill is cheap by
construction rather than by accident.

Dedup still needs a vector when the text did not move; the stored one is reused.

### Identity, authorship and status are not caller-supplied

`id`, `vault_path`, `kind`, `doc_type`, `status`, `doc_status` and `provenance`
come from the existing row. `contributed_by` in particular stays put: it is who
*wrote* the note, and overwriting it with the editor would erase authorship on
every correction. Who edited is what the `vault.update` audit event records.

### 404 does not distinguish missing from unreadable

The lookup applies `READABLE_STATUSES` and the `ai_read` path policy, and a miss
of either kind is the same 404. ADR 0014 keeps the read surface from confirming
that a document exists in a folder the caller may not see; an update surface that
distinguished the two would reopen the channel the dedup query already closes.

`READABLE_STATUSES` moves from `routes.py` to `read_policy.py` so the write path
applies the same rule without importing the transport layer.

### Its own quota bucket

`update` gets `30/min burst 20`, matching `contribute`. A separate bucket rather
than a shared one, so a backfill sweeping the corpus cannot starve new
contributions.

## Consequences

**The facet backfill is now possible but still not runnable.** The blocker moves
from "no mechanism" to "no data": `Vault/00 Governance/Schemas/` has no facet
concept, no Agent Note carries `Aliases`, `Summary` or `SourceIDs`, and
`RelatedIDs` is empty on all 48. The vocabulary decision and an authoring-schema
change come first.

**Flagged documents cannot be corrected through this endpoint.** They are outside
`READABLE_STATUSES`, so an update targeting one is a 404. That is deliberate for
now — adjudicating a flagged document belongs to the review surface, which
`vault:review` is reserved for and which nothing implements. If the review path
lands, it needs its own write, not a widened rule here.

**Last write wins.** There is no optimistic concurrency: no version column, no
`If-Match`. Two editors racing means one silently overwrites the other. The
corpus-wide advisory lock serializes the calls but does not detect the conflict,
because a full replacement has no read-modify-write span on the server to detect
it in. At one sequential importer this is theoretical; it stops being theoretical
the moment a second writer exists, and the fix is a version column compared under
the lock.

**Update does not change `source_sha256`.** A row that is a replica of a Markdown
file would be edited out from under its source without the reconciliation in ADR
0012 noticing, since the hash still describes the file. Today every row carries
NULL — agent-layer rows are DB-authored — so this is latent rather than live, but
human-layer sync must not reuse this endpoint.
