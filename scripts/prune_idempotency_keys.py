"""
Prunes old cumulative-submission dedup markers from submission_idempotency.

STATUS: STUB — implementation deferred (see specs.md Phase 1). The table and
its DELETE grant exist now; the reaper is scoped but intentionally not wired up
until the cumulative write path (Phase 2) lands, so the retention policy gets a
human decision before it runs against any real data.

Why a reaper is needed: submission_idempotency grows unbounded — one row per
distinct (user_id, game_mode, idempotency_key). Unlike refresh tokens it has no
natural expiry, so it needs a time-based reap to preserve the cheap Postgres
tier (same motivation as prune_refresh_tokens / prune_guests).

Retention decision (DEFAULT SUGGESTION — not final): 90 days. The tradeoff: a
replayed submission older than the window is no longer deduped and could
double-count an increment. For a game leaderboard that is acceptable; a far
shorter window risks dropping a legitimate client retry after an outage.

Intended implementation (mirrors prune_refresh_tokens):

    DELETE FROM submission_idempotency
    WHERE first_seen < NOW() - (%s * INTERVAL '1 day')

Usage (once implemented):
    Local:          python -m scripts.prune_idempotency_keys
    Heroku:         heroku run python -m scripts.prune_idempotency_keys --app your-app-name
    Scheduler:      python -m scripts.prune_idempotency_keys (Heroku Scheduler dashboard)

Environment variables:
    DATABASE_URL            Required. Standard connection string.
    IDEMPOTENCY_PRUNE_DAYS  Optional. Markers older than this are eligible. Default: 90.
"""

import logging
import os
import sys

from app.env import load_environment

logger = logging.getLogger(__name__)

DEFAULT_PRUNE_DAYS = 90


def prune_idempotency_keys(prune_days: int = DEFAULT_PRUNE_DAYS) -> int:
    """
    Deletes dedup markers older than prune_days. Returns the number deleted.

    Deferred: the cumulative write path that populates this table does not
    exist yet (Phase 2). Implement against the SQL sketched in the module
    docstring, mirroring prune_refresh_tokens (connect, DELETE, commit,
    rollback-on-error, return rowcount).
    """
    raise NotImplementedError(
        "prune_idempotency_keys is a deferred stub — see module docstring "
        "and specs.md Phase 1. Implement alongside the cumulative write path."
    )


def main() -> None:
    load_environment()

    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    prune_days = int(os.environ.get("IDEMPOTENCY_PRUNE_DAYS", DEFAULT_PRUNE_DAYS))
    logger.info("Pruning idempotency markers older than %d days", prune_days)

    deleted = prune_idempotency_keys(prune_days)

    if deleted == 0:
        logger.info("No eligible idempotency markers found")
    else:
        logger.info("Deleted %d idempotency marker(s)", deleted)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [prune_idempotency_keys] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    main()
