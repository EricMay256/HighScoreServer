# Archived handoffs

Session handoffs and decision briefs from the vault build-out in August 2026.
Each is a snapshot of one moment and none is maintained. They are kept because
the reasoning — what was measured, what was tried and rejected, why a rule
exists — outlives the status lines, which are all stale. Start from
[`../HANDOFF.md`](../HANDOFF.md) and [`../NEXT-STEPS.md`](../NEXT-STEPS.md)
instead.

| File | Written | Holds |
| --- | --- | --- |
| [`HANDOFF-2026-08-13-metadata.md`](HANDOFF-2026-08-13-metadata.md) | 2026-08-13, updated 08-15 | The tags / facets / edge-graph decision brief with the corpus census behind it. Decision 1 (tags stay in the embedding text) is settled; the facet-axis and tag-census questions carried into NEXT-STEPS §3. |
| [`HANDOFF-2026-08-16.md`](HANDOFF-2026-08-16.md) | sessions of 2026-08-12 to 08-16, resolution notes to 08-21 | Calibration becoming a per-model procedure, the digest-version fix, the update and retire endpoints, the code-review findings applied on 08-14 (pre-auth guard, pool split, sampled `touch()`), and the environment gotchas since moved into `HANDOFF.md`. Task 15 there is the rejected "share one connection between auth and handler" idea that `principal.py` still cites. |
| [`HANDOFF-2026-08-19-export-and-compilation.md`](HANDOFF-2026-08-19-export-and-compilation.md) | 2026-08-19 | The four-phase plan for the exporter, retiring the second writer, agent-supplied metadata, and compilation. All shipped; superseded 08-22. |
| [`HANDOFF-2026-08-22-vault-implementation.md`](HANDOFF-2026-08-22-vault-implementation.md) | 2026-08-22, refreshed 08-28 | Context for implementing ADRs 0023–0025, and the conventions learned that session: revision-id length, slowapi against the SDK's CORS-wrapped endpoints, OAuth state in Postgres. |
| [`HANDOFF-2026-08-24.md`](HANDOFF-2026-08-24.md) | 2026-08-24 | The session that merged PR #14, shipped the vault to production, imported the wiki and retired the Stage-A compile loop, with the exporter diff measured against production. |

Code and ADRs that cite `docs/HANDOFF.md` by task number mean
`HANDOFF-2026-08-16.md` here.
