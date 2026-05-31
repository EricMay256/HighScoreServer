"""
Prunes old cumulative-submission dedup markers from submission_idempotency.

submission_idempotency grows unbounded — one row per distinct
(user_id, game_mode, idempotency_key). Unlike refresh tokens it has no natural
expiry, so it needs a time-based reap to keep the table (and the cheap Postgres
tier) bounded — the same motivation as prune_refresh_tokens / prune_guests.

Retention: 30 days by default (override with IDEMPOTENCY_PRUNE_DAYS). The
tradeoff: a replayed submission older than the window is no longer deduped and
could double-count an increment. For a game leaderboard that is acceptable, and
30 days comfortably exceeds any legitimate client-retry horizon.

Usage:
    Local:          python -m scripts.prune_idempotency_keys
    Heroku:         heroku run python -m scripts.prune_idempotency_keys --app your-app-name
    Scheduler:      python -m scripts.prune_idempotency_keys (set in Heroku Scheduler dashboard)

Environment variables:
    DATABASE_URL            Required. Standard connection string.
    IDEMPOTENCY_PRUNE_DAYS  Optional. Markers older than this are eligible. Default: 30.
"""

import logging
import os
import sys

from app.env import load_environment

import psycopg2

logger = logging.getLogger(__name__)

DEFAULT_PRUNE_DAYS = 30


def prune_idempotency_keys(prune_days: int = DEFAULT_PRUNE_DAYS) -> int:
    """
    Deletes dedup markers whose first_seen is older than prune_days.
    Returns the number of markers deleted.
    """
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM submission_idempotency
                WHERE first_seen < NOW() - (%s * INTERVAL '1 day')
                """,
                (prune_days,),
            )
            deleted = cur.rowcount
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("Prune failed, transaction rolled back: %s", e)
        raise
    finally:
        conn.close()

    return deleted


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
