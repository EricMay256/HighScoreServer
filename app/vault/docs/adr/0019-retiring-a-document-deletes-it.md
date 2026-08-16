# 19. Retiring a document deletes it, and the ledger outlives it

Date: 2026-08-14

## Status

Accepted.

## Context

ADR 0018 added a way to change a document. There was still no way to remove one,
and the corpus had reached a state that needs it: a note whose reasoning was
directly contradicted by a later note, giving a reader the argument that twice
led to picking a dedup threshold which would have caught none of the
restatements it was meant to catch.

The markdown engine had the same gap, so the note was removed with `git rm` —
going around the engine, against the vault's own convention. On the service side
nothing removed it at all, so the document stayed `active` and searchable while
its source file no longer existed.

## Decision

**`DELETE /api/v1/vault/notes/{note_id}` removes the row. The write-request
ledger keeps its entry with a null pointer.**

### Deletion, not an archived status

ADR 0008 already has a state for content that is retired: `archived` is withheld
from search but still resolves by id, because "an archived document is retired
but legitimate history and a `related_ids` or `source_ids` reference pointing at
one should still resolve rather than dead-end."

That is exactly right for content that is **superseded but true**, and exactly
wrong for content that is **false**. A row a caller can still resolve is the
failure being fixed, not a record worth keeping. Git history holds the markdown,
and the audit event holds the fact of removal, so nothing that matters is lost.

The two states are not alternatives; they answer different questions. Archive
what has been overtaken. Delete what is wrong.

### Embeddings cascade; the ledger does not

`vault_document_embeddings` already has `ON DELETE CASCADE`, which is correct: a
vector for a document that no longer exists is not a record of anything.

`vault_write_requests` must **not** cascade, and this is the load-bearing part.
That row is what makes a replayed idempotency key a no-op. Delete it and the next
replay of that key finds nothing, treats the request as new, and recreates the
document that was just retired — turning a retry into an undo. Its `document_id`
is nullable, so the pointer is cleared and the row survives, stating exactly what
is true: this key was used, and what it produced is gone.

### A document under review cannot be retired

`vault_review_cases.candidate_document_id` has no cascade either, and a review
case is a durable record of a judgement. A candidate reference therefore blocks
retirement in every review state; otherwise a resolved case would pass the
service check and fail later at the foreign key. Its `similar_documents` JSON
also names the evidence used to reach that judgement but cannot carry a foreign
key. Evidence references block while the case is pending, when deleting one
would destroy unresolved review context. The service applies those two rules
explicitly and refuses with 409 rather than leaking a database error.

In practice a flagged document is already unreachable here: it is outside
`READABLE_STATUSES`, so the lookup 404s first. The check exists for the case
where a case is opened against a document that is still active, and to make the
refusal a stated rule rather than an accident of read policy.

### The audit event is written before the delete, in the same transaction

It has to outlive its subject, and it must not be observable without the deletion
or the reverse. `vault_audit_events` carries no foreign keys (ADR 0002), so the
event survives the row it names.

### 204, and its own tight quota

204 with no body: there is nothing meaningful to say about a document that no
longer exists, and echoing the id back would suggest otherwise.

`retire` gets `10/min burst 5` rather than the `30/20` that `contribute` and
`update` share. Those were widened because contributions arrive in batches; a
retirement should not, and a loop that deletes is worse than a loop that writes.

### Retirement shares the corpus advisory lock

The pending-review check and delete must be atomic with creation of a review case.
Retirement therefore takes the same corpus-wide advisory transaction lock as
contribution and update before checking references. Without it, retirement could
observe no case, a concurrent contribution could record the document as duplicate
evidence, and retirement could then delete that evidence. The serialization cost is
accepted because retirement is deliberately rare and tightly rate-limited.

## Consequences

**Retiring a cited document breaks the wiki, and the service cannot see that.**
A synthesized page citing the document by id gets a dangling `SourceIDs` entry.
The markdown CLI checks for citing pages and refuses without `--force`; the HTTP
endpoint has no equivalent, because wiki pages are not in the database at all
(no `vault_compile_runs` rows exist, and the provenance CHECK requires them). If
the wiki layer is ever projected, this endpoint needs the same guard.

**The two surfaces retire independently and can drift.** Removing a note from
markdown does not remove its document, and vice versa. That is the same gap the
importer has for every other operation — there is no deletion path from markdown
to the service, and ADR 0012's mark-and-sweep is scoped to `Human/%` precisely
because agent rows have no source file. Until reconciliation exists, retiring
means doing it on both surfaces deliberately.

**Deletion is unrecoverable on the service side.** No history table, no soft
delete. The audit event records that a principal retired an id at a time; it does
not record what the document said. If that turns out to matter, the fix is a
retired-documents table written in the same transaction, not a status flag —
because the whole point is that the content stops being resolvable.
