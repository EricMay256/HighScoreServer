# 16. The governed write path

Date: 2026-07-30

## Status

Accepted

## Context

`POST /api/v1/vault/contributions` is the first write surface. Vault ADR 0004
settled that `vault_contrib.core.decide()` and `Policy` are normative and port
verbatim; this records what could *not* be transcribed and had to be decided.

The importer is a knowledge-platform script calling this endpoint, chosen so
that migrating the Agent corpus exercises the real write path rather than a
parallel one. That framing only holds for the Agent layer: those notes were
created through `validate -> dedup -> decide` in the first place, so replaying
them is faithful. Human-layer sync is a different operation — identity comes
from the file path, re-importing an unchanged file must be a no-op, and dedup is
actively wrong, since two human notes on adjacent topics are both legitimate.
That is ADR 0012's mark-and-sweep and is deliberately not this endpoint.

## Decision

### `flag_at` is calibration, not a constant, and does not port

Stage A ships `flag_at = 0.85`. That is a **normalized-title string ratio**.
Here the score is **cosine similarity on `text-embedding-3-small`**, where
unrelated prose routinely exceeds 0.7. Carrying the number across would have
sent a large share of the corpus to review on its first day.

Porting logic verbatim and porting a calibrated constant verbatim are different
acts. `DEFAULT_POLICY` therefore ships `flag_at = 1.0`: only an *identical*
embedding flags. That is not dedup switched off — byte-identical text produces
the same vector and a cosine of 1.0, so exact resubmission is still caught — it
is dedup narrowed to the only band that needs no calibration.

The replacement comes from measuring the pairwise cosine distribution over the
imported corpus, which is only possible *after* the import. The corpus this path
first replays has already passed string dedup, so nothing in it should flag.
Calibrate from the review queue, not from a literature constant.

### One corpus-wide advisory lock

`pg_advisory_xact_lock` over a fixed key, held for the whole critical section.
The dangerous operation is check-dedup-then-write against shared index state:
without the lock, two concurrent contributions both pass dedup and both insert
near-duplicates, and the database accepts them happily. A per-key lock would not
help, because the conflict is between *different* idempotency keys.

This serializes governed writes. At this corpus size that is free, and it is
precisely what makes the dedup decision mean anything.

### Embedding happens before the transaction opens

The source's own migration note predicted this, and it is worth stating why: an
embedding call is a network round trip to a third party. Holding a transaction
across it would pin a pooled connection *and* the advisory lock for the
provider's latency, turning one slow API call into a stall for every writer.

The consequence is a check-then-act window on idempotency, closed by re-reading
the write-request row **under the lock** before deciding.

### No dedup, no write

When no embedding provider is configured the endpoint returns 503 rather than
inserting. The read path degrades to lexical-only and reports it; the write path
must not degrade to *no dedup*, because that silently defeats the single gate
the vault exists to enforce. A refused contribution is recoverable. A corpus
quietly accreting duplicates is not.

### Settled outcomes are 200, including `flagged`

`flagged` and `rejected` return 200 with the disposition in the body. The
request was understood and processed; the note landed and a review case opened.
Reserving non-2xx for transport and authorization failures keeps a client from
treating "queued for adjudication" as an error to retry — which would create a
second note that flags against the first.

Governance validation failure is 422, per the integration spec, and is distinct
from Pydantic's transport-level 422 only in origin.

### The idempotency digest covers the validated model

Not the raw request bytes. Two JSON documents differing in key order or
whitespace are the same request, and treating them as a conflict would refuse a
legitimate retry. A genuinely different body under the same key is 409.

### `contributed_by` comes from the credential

Never from the request body. Taking it from the payload would let one principal
write under another's name, and the audit trail is only worth keeping if the
actor in it is the authenticated one.

### Dedup is scoped by the read policy

`find_similar` applies `readable_path_predicate`. Similarity output names
existing documents, titles them, and scores them, so an unscoped dedup query
would let a contributor learn that a note exists in a folder they may not read
and roughly what it concerns. Dedup quality is not worth a disclosure channel
around ADR 0014.

## Consequences

`Merge` and `Link` raise `NotImplementedError`. ADR 0004 keeps automatic merge
disabled, and `link_at` is unset, so reaching either means a policy set a band
nobody decided on. Failing loudly beats silently doing something plausible.

`Reject` settles the write request as `invalid` — the closest value in
`vault_write_request_state`. `reject_at` is disabled, so this is unreachable
today; if a policy enables it, the enum deserves its own value rather than this
approximation.

The write path is the first code to require an embedding provider in tests.
Every prior route test ran the `not_configured` path, which is how a blank-query
500 survived an entire review. `tests/vault/test_contributions.py` carries a
deterministic stub — identical text yields an identical vector — so the dedup
assertions test the gate rather than coincidence.

**Flagged documents are written, not withheld.** The review queue needs the
content in order to adjudicate it, and ADR 0008 already stops the read surface
from serving a flagged document. A test asserts both halves: the contribution
returns `flagged` with its matches, and fetching that note by ID returns 404.

`vault_write_requests.document_id` and `vault_review_cases.candidate_document_id`
both reference `vault_documents` without cascading, so anything deleting a
document must delete those first. That is correct — an audit trail that vanished
with its subject would not be an audit trail — but it makes cleanup ordering
load-bearing, and it is the reason the contribution tests sweep by contributor
rather than by collected ID.
