# 60. A note may declare what it supersedes, and review archives what it replaced

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented. **Requires a vault Alembic revision.** Adds an
edge to ADR 0025's vocabulary and applies ADR 0030's shape rule to it.

## Context

When a fact changes, the vault can add a new note and retire the old one. Retirement
deletes (ADR 0019), so the history of what used to be believed is lost. Zep/Graphiti
handles contradiction by closing a fact's validity interval rather than deleting it.

The vault already has a state with exactly that meaning. An `archived` note is out of
search but still resolvable by id (ADR 0008).

## Decision

**`supersedes_ids` joins `related_ids` and `source_ids` as a stored, directed edge:
this note replaces those.**

- Shape-checked and never existence-checked, as ADR 0030 requires.
- Declaring it opens nothing automatically. The health report lists pending
  supersessions, and a `vault:review` holder settles each one. Accepting moves the
  old note to `archived`. Declining leaves both active and records the decision.
- The export renders the edge as a wikilink, and the reverse direction is computed at
  export, not stored.

## Consequences

An agent cannot archive another principal's note by declaring an edge; only review
can, which is ADR 0021's boundary. The vault still does not traverse edges (ADR
0025); a reader follows `supersedes_ids` one fetch per hop.
