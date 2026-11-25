# 55. Retrieval tuning is frozen until 500 active readable documents

Date: 2026-11-25

## Status

Accepted 2026-11-25. Adds a precondition to the reopening triggers in ADRs 0031 and 0034.

## Context

Every retrieval measurement in this lineage was taken on 39–94 documents. That covers
the disjunction rule (ADR 0007), the snippet bound and the `ts_headline` analysis
(ADR 0031), chunk eligibility (ADR 0034), and the dedup calibration. At that size
the extremes that thresholds hang on move with each added note.

The whole corpus is roughly 45–60k tokens, and a list of every title and summary is
about 5k. Karpathy's LLM-wiki pattern reports an index file alone working up to
"hundreds of pages". Further tuning would be fitting noise, at a cost in attention
the vault's actual gaps (ADR 0053) need more.

## Decision

**Retrieval tuning is frozen until the vault holds 500 active readable documents.**

Frozen: RRF parameters, the lexical disjunction rule, the snippet bound, a
`ts_headline` arm, chunk-level retrieval, batched multi-query search, and batch fetch
by id.

Not frozen:

- bug fixes;
- the dedup recalibration and similarity warning, which concern the write path;
- validating `retrieval_cases.py`, which is preparation for the unfreeze.

At 500, validate the label set first. Then re-run `measure_retrieval_quality.py` and
`measure_chunk_eligibility.py`, and only then change anything.

## Consequences

If ADR 0031's or 0034's own trigger fires before 500, it still waits for this
re-measurement. "500" counts what `find_similar` and search see: `status = active`
under `readable_path_predicate`.
