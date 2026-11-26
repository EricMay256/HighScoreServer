# 56. The health report is deterministic, and models consume it rather than perform it

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented. Supplies the input the garden routine (ADR 0053)
works from.

## Context

Karpathy's "lint" asks a model to health-check the wiki for contradictions, stale
claims, orphans and gaps. Implementers reported that a model passes drift a script
catches. Several checks this vault needs are already computed by one-off scripts
(`measure_chunk_eligibility.py`, `backfill_vault_summaries.py`, the export's warnings)
and are never collected in one place.

## Decision

**One deterministic report, computed by the service, listing each finding as a check
code, a note id, and a title.** No bodies and no model calls.

Checks:

- notes with no inbound `related_ids` or `source_ids` (orphans);
- dangling edge ids;
- notes missing a summary;
- stale and new-source compile items (the planner's own query);
- tag drift (absorbing the planned tag census);
- notes above the size the corpus treats as oversize;
- stored values containing tool-call markup;
- note pairs whose stored vectors score at or above `similarity_warning_at`;
- pending supersessions (ADR 0060);
- `wrong`/`outdated` feedback (ADR 0059);
- notes unfetched for 90 days, once ADR 0058 exists.

It ships as a script first, then as a read-only MCP tool under `vault:read`. Every
check applies `readable_path_predicate` and excludes the Human collection, because a
report that names notes is a disclosure surface (ADR 0014).

## Consequences

The pairwise similarity check is O(n²) over stored vectors. That is trivial at this
size, and ADR 0055's 500-document review should revisit it. Findings are leads, not
verdicts: the report never edits anything, and the routine that reads it acts only
through governed writes.
