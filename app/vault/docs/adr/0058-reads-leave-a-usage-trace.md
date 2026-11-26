# 58. Reads leave a usage trace

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented. **Requires a vault Alembic revision.**

## Context

Only writes are audited. Nothing records which notes agents actually open, so the vault
cannot tell a note that is useful from one nobody reads. That leaves Karpathy's
append-and-review "gravity" — old, unread items sink, and a review rescues them — with
nothing to work from.

## Decision

**A sidecar table counts fetches.** `vault_document_usage(document_id, fetch_count,
last_fetched_at)`, with an FK to `vault_documents` `ON DELETE CASCADE`, because usage
is derived and dies with its note.

- It is updated on `get_note` from both adapters, outside the read's own transaction.
  A failed update logs its exception type and never fails the read.
- A fetch by the note's own contributing principal within an hour of its creation is
  not counted.
- No query text, ever. No per-fetch rows. `vault_audit_events` is not used, because it
  is unpruned and reads would dominate it.
- Search hits are not counted. A hit is not a use.

The trace feeds review queues and the health report (ADR 0056). **It does not feed
ranking** (ADR 0055).

## Consequences

The first write on the read path. It is best-effort by design, so counts are a lower
bound. Under several workers concurrent fetches contend on one row. An `UPDATE ... SET
fetch_count = fetch_count + 1` keeps that correct, and at this volume it is not a
bottleneck.
