# 61. Promotion means human endorsement, and agents may nominate

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented. Partially supersedes ADR 0023: its mechanics
stand, and the meaning of `promoted` changes. **Requires a vault Alembic revision**:
`vault_amendment_proposals_change_kind_known` admits only `replacement`, `body_diff`
and `metadata`, and the per-kind shape CHECK needs a `promotion` arm.

## Context

ADR 0023 defined `promoted` as "a Human note has been written from it". The Human
rewrite path that meaning served is gone (ADR 0054). Meanwhile ADR 0053's
session-start injection needs a set of notes a human has vouched for, because
injected context reaches every session and an unreviewed note there is a standing
prompt-injection channel. Agents have no way to put a note forward: setting
`promotion_status` requires `vault:review`.

## Decision

**`promoted` means human-endorsed, and eligible for session-start injection.**
`candidate` keeps ADR 0023's meaning, awaiting judgement. `retracted` means the
endorsement was withdrawn or declined.

- **Agents nominate through an amendment proposal** of a new change kind,
  `promotion`, filed under `vault:propose`. It reuses the review surface, revision
  binding and settlement of ADR 0028. Accepting it sets `promotion_status` through
  `VaultPromotionService`, so path routing stays as ADR 0023 built it.
- A read-only `vault_list_promoted` tool under `vault:read` returns ids, titles and
  summaries of promoted notes, optionally filtered by a `project` facet. It is bounded
  to 20 notes and 2 KB, sized for injection.

## Consequences

The injected tier stays small because promotion is a human act. Notes already marked
`promoted` under ADR 0023's meaning should be re-read against the new one before the
SessionStart hook ships.
