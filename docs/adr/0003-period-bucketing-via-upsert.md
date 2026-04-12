# 0003. Period bucketing via upsert

Date: 2026-04-12

## Status

Accepted

## Context

The leaderboard tracks scores across three independent time windows: all-time, weekly, and daily. A naive implementation would store every submission as an append-only row and compute the windows at read time with `WHERE submitted_at >= ...` filters. That works but pushes the period logic into every read query and grows the `scores` table linearly with submissions, most of which will never be a player's best.

A second naive approach is to maintain three separate tables, one per period, with a scheduled job that resets the daily and weekly tables at period boundaries. Scheduled resets introduce a class of bugs around timing precision and dyno restart behavior, and they require a job runner the project would not otherwise need.

What the leaderboard actually wants to store is "the best score each player has achieved in each period window," which is at most one row per `(user, game_mode, period, period_start)` tuple.

## Decision

Each score submission writes to three rows simultaneously — one per period — using `INSERT ... ON CONFLICT (user_id, game_mode, period, period_start) DO UPDATE WHERE` with a conditional that compares the new score against the stored one according to the game mode's sort direction. The `period_start` column anchors each row to its time window: today's date for daily, the start of the ISO week for weekly, a fixed sentinel for all-time. A `UNIQUE` constraint on `(user_id, game_mode, period, period_start)` enforces the one-row-per-window invariant at the database level.

The period start computation lives in `app/periods.py` and is the only place that knows how to derive a `period_start` value from a timestamp.

## Consequences

There is no scheduled reset job and no race condition at period rollover. The first submission after midnight UTC has a new `period_start` value, fails to match any existing row in the daily bucket, and inserts a fresh row naturally. The previous day's row stays in the table as a historical record without interfering with the new day's leaderboard.

Reads are simple: `SELECT ... WHERE game_mode = ? AND period = ? AND period_start = ?` returns exactly the rows for the requested window, with no time filtering required and no risk of off-by-one errors at boundaries.

The load-bearing piece is `app/periods.py`. Any bug in the period start computation — a timezone mistake, a week-start convention mismatch, an off-by-one on month rollover — would silently corrupt bucketing in a way that is hard to detect from the outside. Players would simply find themselves in the wrong bucket. This is why `tests/test_periods.py` exists as a dedicated unit test suite separate from the API integration tests: the function is small but its correctness is disproportionately important.

A secondary consequence is that the `scores` table grows by up to three rows per submission rather than one, but only when the player improves in a given window. Steady-state growth is bounded by `(active players) × (game modes) × (active period windows)`, not by submission rate, which is the right shape for a leaderboard.
