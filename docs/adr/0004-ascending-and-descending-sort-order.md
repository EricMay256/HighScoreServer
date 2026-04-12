# 0004. Ascending and descending sort order as a first-class concept

Date: 2026-04-12

## Status

Accepted

## Context

Different game modes rank scores differently. A points-based mode wants the highest score on top; a speedrun mode wants the lowest time on top. The naive approach is to hardcode "higher is better" everywhere — in the `ORDER BY` of leaderboard queries, in the comparison that decides whether a new submission beats the player's stored best, and in the Unity client's display logic. Adding a speedrun mode to that codebase later is a refactor, not a config change.

## Decision

## Decision


The `game_modes` table has a `sort_order` column with values `ASC` or `DESC`, defined as follows:

- **`DESC`** — higher scores are better. The leaderboard is ordered highest-first, and a submission improves the player's record when the new value is **greater than** the stored value. Used for points, kills, distance, survival time, and similar accumulating metrics.
- **`ASC`** — lower scores are better. The leaderboard is ordered lowest-first, and a submission improves the player's record when the new value is **less than** the stored value. Used for race times, speedruns, stroke counts, and similar "less is more" metrics.

These names match the SQL `ORDER BY` direction the column is substituted into, which is the property that makes the query construction trivial. The semantic meaning ("better") is derived from the sort direction, not the other way around.

Every leaderboard query fetches the sort order for the requested mode before constructing its `ORDER BY` clause and improvement predicate. A speedrun mode and a points mode are the same code path, distinguished only by a single column value in the `game_modes` table. Neither the Unity client nor the web view has hardcoded sort logic — both render whatever the API returns in the order the API returns it.

## Consequences

Adding a new game mode is a single `INSERT` into `game_modes`. The API and clients require no changes. This is the right shape for a portfolio backend that wants to demonstrate it can serve heterogeneous games.

The cost is that every leaderboard query needs to know the sort order before it can build its SQL, which is one extra round trip in the naive implementation. In practice the game mode row is cached alongside other game mode config (`requires_auth`, display label) for the duration of the request handler, so the cost is one fetch per request rather than one per query. Worth knowing because a careless refactor could reintroduce the duplicate fetch without the test suite noticing.

A subtler consequence is that the upsert improvement predicate has to be assembled with the sort direction baked in: `WHERE EXCLUDED.score > scores.score` for `DESC`, `WHERE EXCLUDED.score < scores.score` for `ASC`. The direction cannot be a query parameter — it has to be string-substituted into the SQL before execution. This is a controlled substitution from a known-safe column value, not user input, but it is the one place in the codebase where SQL is built by string concatenation, and it deserves a comment at the call site for anyone reviewing it for injection risk.
