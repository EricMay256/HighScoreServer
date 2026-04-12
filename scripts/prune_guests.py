"""
Prunes guest accounts that have no scores and are older than GUEST_PRUNE_DAYS.

Intentionally does NOT prune guests with scores — that data has value and
requires a separate policy decision. See README for future considerations.

Usage:
    Local:          python -m scripts.prune_guests
    Heroku:         heroku run python -m scripts.prune_guests --app your-app-name
    Scheduler:      python -m scripts.prune_guests (set in Heroku Scheduler dashboard)

Environment variables:
    DATABASE_URL        Required. Standard connection string.
    GUEST_PRUNE_DAYS    Optional. Accounts older than this are eligible. Default: 30.
"""

import logging
import os
import sys

from app.env import load_environment

load_environment()

# Validate DATABASE_URL specifically — we don't need the full app env
if not os.environ.get("DATABASE_URL"):
    print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
    sys.exit(1)

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prune_guests] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)


def prune_guests(prune_days: int = 30) -> int:
    """
    Deletes guest accounts with no scores older than prune_days.
    Returns the number of accounts deleted.

    The NOT EXISTS subquery ensures we never touch a guest with scores.
    The ON DELETE RESTRICT on scores is a secondary safety
    net — the DB will refuse the delete if a score row exists regardless.
    """
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM users
                WHERE is_guest    = TRUE
                  AND created_at  < NOW() - (%s * INTERVAL '1 day')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scores s
                      WHERE s.user_id = users.id
                  )
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


if __name__ == "__main__":
    prune_days = int(os.environ.get("GUEST_PRUNE_DAYS", 30))
    logger.info("Pruning guest accounts with no scores older than %d days", prune_days)

    deleted = prune_guests(prune_days)

    if deleted == 0:
        logger.info("No eligible guest accounts found")
    else:
        logger.info("Deleted %d guest account(s)", deleted)