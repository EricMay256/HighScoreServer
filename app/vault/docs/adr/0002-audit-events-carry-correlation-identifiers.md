# 2. Audit events carry correlation identifiers, not foreign keys

Date: 2026-07-28

## Status

Accepted

## Context

The first draft of `vault_audit_events` carried a composite foreign key on
`(principal_id, idempotency_key)` referencing `vault_write_requests`, plus a check constraint
requiring `principal_id` to be present whenever `idempotency_key` was.

The intent was sound — audit rows should be traceable to the write they describe — but a
foreign key is the wrong mechanism for an audit log, for three independent reasons.

**An audit insert must not be able to fail.** A foreign key gives the audit write a way to
raise a referential error. The moments when the log matters most are exactly the disordered
ones: a write rejected before its request row was created, a partially applied transaction, a
replay against a pruned key. A log that declines to record those events is not a log.

**It discards correlation precisely where it is most valuable.** Events for rejected or
unauthenticated writes have no matching `vault_write_requests` row, so under the constraint
they must null their `idempotency_key`. The failed and unauthorized attempts — the security
interesting ones — would be the only events stripped of the identifier needed to correlate
them.

**It welds the two tables together for life.** With the foreign key in place,
`vault_write_requests` can never be pruned while any audit event references it. Idempotency
keys are inherently short-lived operational records; HSS already ships
`scripts/prune_idempotency_keys.py` for exactly this table shape on the leaderboard side. The
audit log, by contrast, is meant to be long-lived. Coupling their lifetimes forces the
shorter-lived table to adopt the retention of the longer-lived one.

The same reasoning was already accepted for `target_id`, which is deliberately unconstrained
so audit history survives deletion of the row it describes.

A related question was `latency_ms NOT NULL`. Not every audited event is a request with a
duration — lifecycle and system-generated events have none — and a non-null constraint forces
a fabricated zero that is indistinguishable from a genuinely instant operation.

## Decision

`vault_audit_events` stores `principal_id` and `idempotency_key` as plain correlation
identifiers with no foreign key to `vault_write_requests`, and no constraint tying one to the
presence of the other.

The format check on `idempotency_key` is kept, so a recorded key is still well-formed. The
`target_type`/`target_id` consistency check is kept for the same reason.

Because the foreign key's implicit index is gone, a partial index
`idx_vault_audit_events_principal_idempotency` on `(principal_id, idempotency_key)
WHERE idempotency_key IS NOT NULL` preserves cheap "which events belong to this write
request" lookups.

`latency_ms` becomes nullable. The `latency_ms >= 0` check is retained and tolerates NULL
correctly, so real values remain constrained.

## Consequences

Audit inserts cannot fail on referential integrity. Events for rejected, unauthenticated, and
replayed writes retain their correlation key. `vault_write_requests` can be pruned on its own
schedule without touching audit history.

The cost is that correlation is now advisory: an `idempotency_key` in the audit log may point
at a write request that no longer exists, or never did. This is the correct trade for a log —
the identifier records what was claimed at the time, which is what an audit trail is for — but
it means joins from audit events to write requests must be outer joins, and consumers must
handle unmatched rows.

Nothing enforces at write time that a correlation identifier is well-formed beyond its regex.
Application code is responsible for populating it consistently.

## Amendment, 2026-08-23 — an unauthenticated endpoint does not get to write here

This record has no retention, which is the point of it. That makes "who may cause a row" a
question this ADR has to answer, and it did not.

Vault ADR 0024's `/register` is public and unauthenticated by specification, and it was
recording an audit event per call. A caller with no credential could therefore write unbounded
permanent rows into the durable record, limited only by a rate limit — which slows accumulation
rather than bounding it. That is a storage-exhaustion path dressed as an audit trail, and it
also dilutes the trail with events no operator asked for.

**The rule: an audit event records an action on the corpus or on a credential, taken by an
identified principal.** An unauthenticated call that grants nothing and changes nothing gets a
structured log and, where the fact needs to persist, a row in its own table with its own
retention. Registration now does exactly that — `vault_oauth_clients.registered_at` holds the
fact, and pruning covers it.
