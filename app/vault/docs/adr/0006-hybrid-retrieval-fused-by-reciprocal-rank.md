# 6. Hybrid retrieval fused by reciprocal rank

Date: 2026-07-28

## Status

Accepted, amended by [ADR 0007](0007-lexical-arm-disjoins-query-terms.md).

Everything below stands except the construction of the lexical `tsquery`. This ADR left the
lexical arm conjunctive, which made its recall fall away as queries lengthened — so on the long,
question-shaped queries this corpus exists to answer, the arm contributed far less than the text
below assumes. ADR 0007 disjoins the terms. Fusion, `k`, oversampling, and the degradation policy
are unaffected.

## Context

The read-only slice has two independent ways to find a document: the persisted `search_vector`
generated column with its GIN index, and cosine similarity over `vault_document_embeddings`.
They fail in opposite directions. Lexical search misses paraphrases and synonyms but is exact
on identifiers, error strings, and rare proper nouns — precisely the content a technical
knowledge corpus is full of. Vector search handles paraphrase but blurs exact tokens and can
rank a topically adjacent note above a literal match.

Combining them requires deciding how. A weighted sum of `ts_rank_cd` and cosine similarity is
the obvious approach and the wrong one here: the two scores are on unrelated scales, neither is
calibrated across queries, and choosing weights needs relevance judgements that do not exist for
this corpus. Any weighting chosen now would be a guess presented as a tuning parameter.

A further constraint is that the vector arm depends on a third-party provider being reachable,
while the lexical arm depends only on PostgreSQL. Treating an embedding failure as a search
failure would make an external outage take down retrieval that could still have answered.

## Decision

Both arms run independently and are combined with **Reciprocal Rank Fusion**, `k = 60`. Each
arm contributes `1 / (k + rank)` for the documents it ranks, positions starting at 1, and the
summed score orders the result.

Fusion consumes **positions, not magnitudes**, so no cross-scale calibration is needed and there
are no weights to tune. Each arm oversamples to four times the requested page depth, capped at
200, so a document ranked well by only one arm still survives into the fused page.

`reciprocal_rank_fusion` is a pure function over ID lists, unit-tested without a database.

**A failure to embed the query degrades the search to lexical-only rather than failing it.** The
response carries `profile_id` and a three-valued `vector_status` — `used`, `not_configured`, or
`failed` — so the caller is told both that the answer is narrower than usual and *why*.

The three-way split matters more than it looks. A plain boolean would report an outage and a
deliberate lexical-only deployment identically, and those need opposite reactions: one is an
incident, the other is how CI and local development are meant to run. For the same reason
`failed` logs at ERROR while `not_configured` logs nothing per request — otherwise a supported
configuration would bury real faults in noise.

The query text is never logged, and neither is an embedding exception's message, since both can
carry user content; the exception type is recorded instead.

Both arms filter to `status = 'active'`, and the lexical arm passes the text search
configuration as a **bound parameter** to `websearch_to_tsquery`, never the database's
`default_text_search_config` and never string interpolation. (ADR 0007 rewrites that query's
conjunctions into disjunctions; the bound-parameter rule is unchanged.)

## Consequences

Retrieval needs no relevance-judgement corpus to be reasonable on day one, and the ranking has
no magic constants beyond `k`, whose effect is documented and bounded. Because fusion is
position-based, replacing the embedding profile changes which documents the vector arm proposes
but requires no re-tuning of the combination.

Two round trips are issued per search instead of one. At this corpus size that is not the
bottleneck; if it becomes one, the two statements can be folded into a single CTE without
changing the fusion semantics.

Vector search has **no relevance floor**: kNN returns the nearest *k* documents whether or not
any of them are close. A document can therefore appear in results on vector evidence alone while
being only loosely related. RRF damps this — a weak vector-only hit sits below anything both
arms agree on — but it does not eliminate it. (ADR 0007 notes that agreement between the arms is
a weaker signal than this framing implies, once the lexical arm is disjunctive.) Introducing a
distance threshold is deferred until
there is evidence about what threshold would be right, since a badly chosen one silently removes
correct answers.

Degrading to lexical-only is a deliberate quality reduction that callers must be able to detect,
which is why it is reported in the response body rather than logged and forgotten.

The vector arm's `profile_id` predicate remains a post-filter under the unpartitioned HNSW index
described in ADR 0003. With one populated profile this costs nothing; the partial-index remedy
belongs to the migration that populates a second.
