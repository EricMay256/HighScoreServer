# 13. Cumulative scoring via idempotency keys

Date: 2026-05-31

## Status

Accepted

## Context

Some game modes want to *accumulate* score across submissions (coins collected
over many sessions) rather than keep a personal best. Accumulation has a hazard
that best-wins does not: a client retry or a replayed request double-counts the
increment. Best-wins is naturally idempotent (re-submitting the same best is a
no-op); summing is not.

Two questions: how a mode opts into accumulation, and how to make an increment
apply at most once. A tempting coupling was to tie dedup to validation — only
count an increment if it arrives as a validated run. But that would deny
accumulation to simple modes that don't need validation at all, conflating two
independent concerns.

## Decision

Make cumulative a **per-mode scoring strategy**, deduped by an **idempotency
key independent of validation**.

- `game_modes.scoring_strategy` is `'best'` (default) or `'cumulative'`
  (CHECK-constrained). The shared write tail branches on it: `best` keeps the
  improvement-gated upsert; `cumulative` does `score = score + EXCLUDED.score`
  with no improvement gate, treating the submitted value as the increment.
- A cumulative submission **requires an idempotency key**; the requirement is
  validated in the handler against the looked-up mode (it is data-dependent and
  can't be expressed in the model alone). A missing key on a cumulative mode is
  a 422.
- Dedup is decoupled from validation, so cumulative works at **any tier**,
  including tier 0 (raw `/scores`). The increment and the dedup marker are
  written **in the same transaction**, so a crash can't apply one without the
  other.
- Two dedup mechanisms, one per path, chosen to avoid a redundant double-write:
  - **raw cumulative** (`/scores`, no run) uses a `submission_idempotency`
    table — `INSERT ... ON CONFLICT DO NOTHING`; a conflict means the whole
    write is a no-op.
  - **run-based cumulative** (`/runs`) reuses `runs.client_run_id` UNIQUE for
    anti-replay (the endpoint returns the prior result before reaching the write
    tail), so it does **not** also write `submission_idempotency`.

Each period bucket accumulates independently — this falls out of the existing
`period_start` mechanic, so daily resets daily and all-time never resets with no
extra logic.

## Consequences

### Positive

- Accumulation is available to any mode at any tier, not gated behind
  validation; a simple raw mode can sum increments.
- Retries and replays are safe: an increment applies at most once per key, and
  the marker + increment share a transaction.
- One dedup mechanism per path with no redundancy — the open "reuse
  `runs.client_run_id` vs. a unified table" question is resolved in favor of not
  double-writing.

### Negative

- `submission_idempotency` grows unbounded (one row per distinct
  `(user, mode, key)`) and needs reaping — `scripts/prune_idempotency_keys.py`
  deletes markers older than `IDEMPOTENCY_PRUNE_DAYS` (default 30). The
  tradeoff: a replay older than the retention window is no longer deduped and
  could double-count. Accepted for a game leaderboard; 30 days exceeds any
  legitimate retry horizon.
- Cumulative places a real requirement on clients: they must supply a stable,
  unique idempotency key per logical increment (and, for runs, a stable
  `client_run_id`). A buggy client that reuses a key drops increments; one that
  never repeats a key on retry double-counts.

### Neutral

- `scoring_strategy` defaults to `'best'`, so existing modes are unaffected; it
  is surfaced read-only on `GameModeConfig`.
- The shared write tail (see ADR 0010) is the single place the best/cumulative
  branch lives, so both `/scores` and `/runs` accumulate identically.
