# 10. Two submission endpoints over a polymorphic one

Date: 2026-05-31

## Status

Accepted

## Context

The validated-runs feature introduces a second way to put a score on the
leaderboard. Alongside the existing raw submission (`POST /api/leaderboard/scores`,
a `{score, game_mode}` body that upserts directly), a *run* can be submitted: a
full scenario-version + seed + action log that the server validates and from
which it computes the canonical score.

Two API shapes were available:

1. **One polymorphic endpoint.** Keep `POST /scores` and branch on the payload
   (or the mode's tier): a bare score takes the raw path, a run-shaped body
   takes the validation path.
2. **Two endpoints.** `POST /scores` for raw submissions and a distinct
   `POST /runs` for validated runs.

The two paths differ in more than payload. A run has a lifecycle a bare score
does not (`pending → validated | rejected`); it carries an opaque action-log
blob; its server-side cost is dominated by validation, not by the upsert; and it
warrants a different rate limit because that validation is potentially expensive.

## Decision

Adopt **two endpoints**: `POST /api/leaderboard/scores` (raw) and
`POST /api/leaderboard/runs` (validated).

- A run is modeled as a distinct resource that *produces* a score, not as a
  variant of a score. `/runs` owns run persistence, validation, and the
  `pending/validated/rejected` lifecycle; `/scores` stays the cheap direct
  upsert.
- The two paths carry **independent rate limits** (`/scores` 10/min, `/runs`
  5/min) so the expensive path can be throttled without starving the cheap one,
  and so an operator can reason about each in isolation.
- **A single shared write tail** (`_apply_score_write`) is the one place that
  performs the per-period upsert and the best-vs-cumulative branch, so two entry
  points do not mean two implementations of write semantics.
- **Wrong-endpoint submissions get a guided 409**, not a generic error: a raw
  submission to a run-required mode returns `code=RUN_REQUIRED, submit_to=/runs`;
  a run to a raw mode returns `code=RAW_ONLY, submit_to=/scores`. The body keeps
  `detail` a human string with `code`/`submit_to` as top-level siblings.

The textbook alternative — a polymorphic endpoint with a union request type —
was rejected because it couples two different cost/latency/lifecycle profiles
behind one route, makes `/docs` describe a conditional payload instead of two
clear schemas, and forces one rate limit to cover both the cheap and the
expensive path.

## Consequences

### Positive

- Clean resource modeling: a run is a thing with a lifecycle, surfaced as its
  own resource. The response shape is still the unified `ScoreResponse`, so
  clients get one result type from either path.
- Independent rate limits and cost isolation; the validation path can be tuned
  or temporarily tightened without touching raw submission.
- `/docs` stays self-documenting — each route has exactly one request and one
  response schema.
- The shared write tail keeps write semantics single-sourced despite the two
  entry points, so best/cumulative/period behavior can't drift between them.

### Negative

- Two endpoints to maintain and to document, and clients must know which to
  call. Mitigated by the guided 409 (machine-routable `submit_to`) and by
  surfacing `required_tier` read-only on `GameModeConfig`.
- Reconfiguring an *existing* mode to `required_tier > 0` is a deliberate break
  for pre-run clients: their raw `POST /scores` starts returning 409. This is
  the one client-visible breaking change and is surfaced, not silent.

### Neutral

- The cross-routing contract (string `detail` + sibling `code`/`submit_to`) is
  shaped to the hss-unity client's existing `TryExtractDetail`, which handles a
  string `detail` cleanly and would stringify a nested object.
- Idempotency/anti-replay lives on `/runs` via `runs.client_run_id` UNIQUE; the
  raw path's cumulative dedup uses a separate mechanism (see ADR 0013).
