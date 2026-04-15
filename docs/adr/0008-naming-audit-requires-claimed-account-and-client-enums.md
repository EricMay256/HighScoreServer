# 8. Naming audit: requires_claimed_account and client-side enums

Date: 2026-04-15

## Status

Accepted

## Context

After the codebase stabilized through Phases 1–6, a directed naming audit
surfaced two identifiers that describe the wrong layer of the stack or that
encourage runtime errors a type system could catch at compile time. Both
were carried as known candidates from earlier phases and are addressed
together because they share a theme — stringly-typed or misleadingly-named
wire values — and because batching them into a single coordinated pass keeps
the migration story coherent.

### Candidate 1: `game_modes.requires_auth`

The column gates whether a game mode accepts score submissions from guest
accounts. The name is misleading because every score-submitting request
already requires authentication — guest users are authenticated, they just
hold an unclaimed account. The flag is asking "does this mode require a
*claimed* (non-guest) account," which is what the name should say.

A reader opening `schema.sql` cold could reasonably interpret
`requires_auth = TRUE` as "this mode is the only one that requires a
Bearer token," which is the opposite of how the system works. The
misreading is structural, not stylistic — it comes from the name pointing
at the wrong layer.

### Candidate 2: stringly-typed enum-like values on the C# client

Two values cross the wire as strings with a small fixed domain:

- `period` — request parameter on `GET /api/leaderboard/scores`, server-side
  constrained to `Literal["alltime", "daily", "weekly"]`
- `sort_order` — field on `GameModeConfig` responses, server-side constrained
  by regex to `^(ASC|DESC)$`

Both are held as `string` in the Unity client. A typo in either is
discoverable only at runtime: `period: "wekly"` returns 422 from the server,
and an invalid `sort_order` would never originate client-side but cannot
be branched on safely without a string compare. The C# client already has
one enum (`ApiErrorKind`) for the same anti-pattern on the response side,
so the precedent for promoting these is established.

## Decision

Rename `game_modes.requires_auth` to `requires_claimed_account` across the
schema, server code, Pydantic models, Unity client models, and
documentation. Promote `period` and `sort_order` to C# enums in the Unity
client. The wire format remains unchanged: enums serialize to and
deserialize from the same strings the server already exchanges.

### Rollout for the column rename: one-step

The migration is a single `ALTER TABLE game_modes RENAME COLUMN
requires_auth TO requires_claimed_account` paired with a code deploy that
references the new name. There is a brief window between the migration
applying and the new code starting to serve traffic during which the
running code references a column that no longer exists; at Heroku
dyno-restart speed this is a few seconds.

The textbook zero-downtime alternative is a two-step rename: add the new
column, backfill, install a sync trigger, deploy code that reads the new
name, then in a follow-up deploy drop the trigger and the old column. This
is the correct pattern for systems with an SLA that cannot tolerate any
write-path interruption.

The one-step path is taken because this project runs on a single Heroku
dyno with no SLA and no concurrent writers that would observe an
inconsistent intermediate state. The two-step path is documented here so
that if this codebase or pattern is ever applied to a system with
availability requirements, the scaled-up version is on record.

### Rollout for the C# enums: client-only

No server change. The enums are defined with explicit string mappings
matching the wire format. `Period.Alltime` serializes to `"alltime"`,
`SortOrder.Asc` to `"ASC"`, and so on. Existing callers that pass strings
need to be updated to pass enum values; the compiler surfaces every
callsite.

## Consequences

### Positive

- `requires_claimed_account` reads correctly when written out in prose:
  "this game mode requires a claimed account" matches the runtime check.
- The rename eliminates a category of misreading that a reviewer would 
  probe: the column name no longer suggests a relationship to
  authentication that the system doesn't have.
- Promoting `Period` and `SortOrder` to enums catches typos at compile
  time. The Unity client gains internal consistency with `ApiErrorKind`,
  which already follows this pattern.

### Negative

- The one-step migration introduces a brief write-path interruption.
  Acceptable at current scale; explicitly not acceptable for systems with
  uptime requirements.
- `SortOrder` is response-only metadata the client receives but does not
  branch on. Promoting it to an enum gains less than promoting `Period`
  (which is a request parameter the client actively constructs). The
  symmetry was chosen deliberately to treat the same anti-pattern
  consistently rather than asymmetrically, but the cost is that
  `SortOrder` enum maintenance (minor) is overhead for a value the client
  forwards without inspecting.
- The C# `SortOrder` enum's valid values are dictated by a server-side
  regex constraint (`^(ASC|DESC)$`). If the server ever adds a third sort
  mode, the client enum drifts silently and deserialization will fail at
  runtime. The wire contract is the source of truth; the enum is a
  convenience that mirrors it. This is the same coupling that already
  exists between `app/periods.py:PERIODS` and the Pydantic `Literal` —
  documented in the existing `# Maintain against` comment — and the
  mitigation is the same: any change to the wire-format domain requires
  updating both sides.

### Neutral

- ADR 0001 is updated to reflect the name change to `requires_claimed_account` from
  `requires_auth` with a forward reference to ADR 0008. The
  decision in ADR 0001 (guest accounts over nullable foreign keys) is
  unchanged; the edit is naming-only and is recorded so the
  decision log reads accurately.
- The wire format does not change. External consumers (the web view, any
  hypothetical future API clients) that read `requires_claimed_account`
  from `GET /api/leaderboard/game_modes` will see the new key; clients
  that haven't been updated will see a missing field. The Unity client
  is the only known external consumer and is updated in the same pass.