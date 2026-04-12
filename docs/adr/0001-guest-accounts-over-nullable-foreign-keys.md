# 0001. Guest accounts over nullable foreign keys

Date: 2026-04-12

## Status

Accepted

## Context

The application needs to accept score submissions from players who have not registered an account. The conventional approach to anonymous submission in a leaderboard schema is a nullable `user_id` column on the `scores` table, with anonymous rows owning no user. This creates a class of orphan scores and complicates every query that joins scores to users — every join becomes a `LEFT JOIN`, every projection has to handle `NULL` usernames, and account claiming has no clean migration path because there is no row to update.

The Unity client's UX requirement reinforces the problem: players should be able to submit a score on first launch with no login screen. Whatever the schema looks like, the client cannot be asked to wait on a registration form before the first score submission works.

## Decision

Every score submission requires an authenticated user, but authentication is silent on first launch. The Unity client calls `POST /api/auth/guest` automatically, the server creates a real `users` row with `is_guest=true` and a generated username, and returns a JWT plus refresh token. The client stores the tokens in `PlayerPrefs` and from that point is indistinguishable from a registered user at the API layer. Guest accounts can be upgraded to claimed accounts later via `POST /api/auth/claim`, which updates the existing row in place rather than migrating data.

## Consequences

Every query joining `scores` to `users` is an unconditional `INNER JOIN` with no `NULL` handling. The `scores.user_id` foreign key is non-nullable and `ON DELETE RESTRICT`, so leaderboard history cannot be accidentally orphaned. Account claiming is a single `UPDATE` on the `users` table — no score rows move, no IDs change, the player's history is preserved by construction.

The cost is that the `users` table accumulates guest rows that may never become real users. This is the problem `scripts/prune_guests.py` exists to solve: it deletes `is_guest=true` rows older than `GUEST_PRUNE_DAYS` that have no associated scores. Guests who actually played and left a score are preserved indefinitely, since their rows are load-bearing for the leaderboard.

A second consequence worth naming: the `is_guest` flag becomes an authorization input, not just a label. Game modes can set `requires_auth=true` to block guest submission, which is the right shape for modes where the player identity matters (a tournament leaderboard, say) but adds a small amount of branching to the score submission path.
