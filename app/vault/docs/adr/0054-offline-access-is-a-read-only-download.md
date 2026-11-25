# 54. Offline access is a read-only download; the Obsidian sync client is not built

Date: 2026-11-25

## Status

Accepted 2026-11-25. Partially supersedes ADR 0048: its Obsidian extension, already
optional by its 2026-09-12 amendment, is now not built. ADRs 0049–0052 (phase B, built)
stand unchanged. Phase C (Human browser authoring) and phase E (daily Human indexing)
are not decided here.

## Context

ADR 0048 paired database authority for Human notes with two clients: browser
authoring and an Obsidian extension for bidirectional offline sync. Its amendment
separated the two halves of the extension. Offline editing needed the full sync
protocol. Keeping local copies current needed only a one-way projection.

The operator has since judged the web interface sufficient for authoring. What is
wanted offline is reading, with no contribution.

## Decision

**Offline access is a downloadable, read-only snapshot. Nothing offline writes back.**

- The snapshot is the exporter's projection, packaged for viewing. No sync client, no
  conflict handling, and no `clients/obsidian/` directory.
- **A download contains exactly what the downloading credential could read through the
  API, and no more.** Agent paths follow `ai_read` (ADR 0014). Human notes appear only
  for a credential holding `vault:human-read` (ADR 0050). The download is a second
  transport for existing reads, not a new audience.
- It is served by the `vault:export` surface, whose scope and `snapshot` quota already
  exist and whose route does not. Until that route is built, the operator runs
  `scripts/export_vault_markdown.py`.

## Consequences

- The sync specification's client half and the extension sections of
  `human-vault-handoff.md` are superseded. The change feed, revisions and tombstones
  (ADRs 0049, 0052) stay as built. A future incremental download may consume them.
- Local Markdown is a cache that may be stale. Deleting or editing it does nothing to
  the server, by design.
- Whether to build phase C, and on what timetable, is still open and should be decided
  explicitly rather than inferred from this ADR.
