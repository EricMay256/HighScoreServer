# 16. The governed write path

Date: 2026-07-30

## Status

Accepted. Amended 2026-08-12: the `flag_at` section's reasoning was replaced with
measurement. The decision it reached — `flag_at = 1.0` — is unchanged, so this is
an amendment in place rather than a superseding ADR. See "Amendment" below.

Amended 2026-08-13: "The idempotency digest covers the validated model" is
narrowed to *the fields the caller supplied*, and stored digests now record the
rule that produced them. The decision's intent is unchanged — key order and
whitespace still must not make a conflict — so this too is an amendment in place.
See "Amendment, 2026-08-13" below.

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
Here the score is **cosine similarity on an embedding model**. Those are
different scales measured over different things, so the number does not carry
across — porting logic verbatim and porting a calibrated constant verbatim are
different acts.

`DEFAULT_POLICY` therefore ships `flag_at = 1.0`: only an *identical* embedding
flags. That is not dedup switched off — byte-identical text produces the same
vector and a cosine of 1.0, so exact resubmission is still caught — it is dedup
narrowed to the only band that needs no calibration.

A replacement value must be **derived by measurement, per model**, and the
procedure is `app/vault/docs/embedding-calibration.md`. Until a model has a row
in that register, its `flag_at` is 1.0.

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

**Narrowed 2026-08-13** to the fields the caller supplied. See the amendment
below: hashing the whole model made the digest depend on the server's schema.

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

## Amendment, 2026-08-12

The original `flag_at` section reached the right decision from a premise that was
never measured. It asserted that "unrelated prose routinely exceeds 0.7 cosine"
and that carrying Stage A's 0.85 across "would have sent a large share of the
corpus to review on its first day." Both claims are now measured, and both are
wrong — in opposite directions, which is why the conclusion survived.

Over 39 imported Agent notes (741 pairs) on `text-embedding-3-small`: p50 0.2542,
p99 0.6265, max 0.7406. Unrelated prose does **not** routinely exceed 0.7, and
`flag_at = 0.85` would have flagged nothing at all rather than a large share.

The reasoning that replaced it is the more important correction. Reading only that
distribution suggests a wide empty band between 0.7406 and 1.0 in which a
threshold could safely sit — and the first draft of this amendment proposed 0.85
on exactly that basis. Measuring the *other* side refutes it: deliberate
restatements of a single insight in different words score 0.7500, 0.7664, and
0.8431. They sit inside the supposedly empty band. A threshold of 0.85 catches
none of them; 0.80 catches one.

The band looked empty because nothing had been measured in it.

So the corpus distribution bounds only the false-positive side. Deriving a
threshold needs an opposing measurement — known duplicates — and a value sits
between the two only if they are far enough apart to survive further sampling.
Here they are not: floor 0.7406, ceiling 0.7500, a gap of 0.0094. The closest
legitimately-distinct pair in the corpus and the weakest deliberate duplicate are
within 0.01 of each other. `text-embedding-3-small` does not separate
restatement from adjacency on a corpus of short operational notes.

### A second methodological correction, same day

The first version of the measurement compared the two sides on **different text
shapes**, and that biased it. Corpus scores come from stored vectors, which
`assemble_embedding_text` built over title + aliases + tags + summary + body;
the reference pairs were embedded as bare body prose, with no title line and no
tags.

Tags are not a rounding error in that comparison. Re-embedding fourteen corpus
documents with tags removed moved the *maximum* pair by −0.0513 while moving the
mean only −0.0099 — tags disproportionately inflate exactly the top pairs,
because the pairs that share tags are the pairs already topically close. One pair
sharing `git`, `gotcha`, and `tooling` fell 0.0995.

So the floor was tag-inflated and the ceiling was not, which understated the
margin. The reference pairs are now full note shapes — title, body, and
overlapping-but-not-identical tags — and both sides run through
`assemble_embedding_text`.

Correcting it moved the ceiling from 0.7478 to 0.7500 and the margin from 0.0072
to 0.0094. **The verdict is unchanged**: still far under `MINIMUM_SEPARATION`,
still `flag_at = 1.0`. Notably the fix did not uniformly inflate the ceiling —
one pair rose 0.035, another *fell* 0.038 because its two titles differ more than
its bodies do — which is the behaviour a fixture that is not flattering itself
should show.

This leaves an open question for ADR 0013 rather than answering it: if tags
inflate the top of the corpus distribution by ~0.05, and topical tags like
`gotcha` or `tooling` will never become facets under ADR 0017, then **whether
`tags` belongs in the embedding text at all** is now a measurable question that
directly governs how calibratable `flag_at` can ever be.

`flag_at` therefore stays 1.0, now for a measured reason rather than a placeholder
one. "Calibrate from the review queue" is superseded by the two-sided procedure in
`app/vault/docs/embedding-calibration.md`, which does not require a review queue
to have accumulated first, and which must be re-run per model — the threshold is a
property of the model and corpus together, not a vault-wide constant.

`app/vault/calibration.py` carries the reference pairs and the derivation;
`scripts/measure_dedup_similarity.py` runs it; the per-model results live in the
register. `tests/vault/test_calibration.py` keeps the derivation honest, including
the case that nearly shipped a threshold below its own floor.

## Amendment, 2026-08-13

The digest hashed the validated model with `exclude_none=False`, which covered
every field the model *declared* — including ones the caller never sent,
serialized at their defaults. That made the digest a function of the server's
schema as well as of the request.

`5bdd5ad` added `summary`, `aliases`, `facets`, `related_ids` and `source_ids`
to `VaultContributionRequest`. Every one is optional and backward compatible, and
between them they changed the digest of every request that had ever been made.
On 2026-08-13 the corpus importer replayed 48 unchanged notes; the 39 already
present returned 409 with byte-identical payloads on the wire. Nothing had
drifted except the schema, and the error blamed the client.

The general form is worse than the instance: under the old rule *any* additive
field addition silently invalidates every idempotency record in the table, and
does so at the next replay rather than at deploy, so the deploy that causes it
looks clean.

**Two changes, together.** The digest covers only fields the caller supplied
(`exclude_unset=True`), which is stable across additive schema change and keeps
the key-order and whitespace property the original decision wanted. And
`vault_write_requests.digest_version` records which rule produced a stored
digest, because that question has to be answerable per row: stored digests are
not recomputable, since the payloads that produced them were never kept.

`REQUEST_DIGEST_VERSION` in `app/vault/service.py` is the current rule. Changing
`_canonical_request_digest` means bumping it.

**A version mismatch replays without comparing.** When a stored digest came from
a retired rule, it is not evidence about the body — it is an absence of evidence,
and refusing on it would strand every pre-migration key on a 409 no caller could
clear. The replay is logged rather than silent. This is a deliberate, bounded
weakening: it applies only to rows written before migration `0006`, which is the
48 imported notes and nothing else. Keys written under the current rule compare
exactly, and a mismatch there is still 409 — `tests/vault/test_contributions.py`
asserts both halves so the grandfather clause cannot quietly widen.

**The replay restates the digest under the current rule**, so a row is
uncomparable for one call rather than for the rest of its life. Grandfathering
without restating was the first implementation and was wrong: it made the
concession permanent where a two-column write makes it self-healing. The
invariant that matters is the one the code asserts — a replay must buy neither an
embedding call nor a second document — and restating a digest does neither.

Where two different bodies race the first post-migration replay of one key, the
last write wins. That is the same "the first request after the migration is taken
as canonical" property the grandfather clause already had rather than a new one,
and it needs two clients sharing a key, which is already pathological.

What a version mismatch deliberately does *not* do is overwrite the stored
**document**. That is tempting, because it looks like a free update path for
exactly the rows that need one. Retry-after-timeout is the canonical reason
idempotency keys exist: a client that never learned the outcome retries,
sometimes from a queue, sometimes carrying a payload older than the one that
actually landed. Under "newer wins" that retry silently replaces current content
with stale content, and `vault_documents` keeps no history to recover from. It
would also bypass the dedup gate — or run it against the very row being updated,
which flags at cosine 1.0 — and force a re-embed on a path documented as free.
Overwrite belongs in an update operation where it is the caller's stated intent,
not in a retry.

**What this does not fix.** There is still no way to *change* a document through
the write surface. A replay returns the stored response and a conflict refuses;
neither carries new field values onto an existing row. So the 48 imported
documents cannot receive `facets`, `summary`, `aliases`, `related_ids` or
`source_ids` by re-running the importer, however wide its payload gets. That is
a separate decision — a distinct update endpoint keyed on document id, or an
opt-in "replay may update when the body differs" — and it should be made before
the importer is taught the fuller contract, because widening the payload without
it changes the digest again and buys nothing.
