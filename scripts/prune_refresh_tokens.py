"""
Prunes expired refresh tokens from the refresh_tokens table.

Refresh tokens past their expires_at are dead weight — they can't mint new
access tokens regardless of whether they're still in the table. Keeping the
table bounded preserves the cheap Postgres tier.

Usage:
    Local:          python -m scripts.prune_refresh_tokens
    Heroku:         heroku run python -m scripts.prune_refresh_tokens --app your-app-name
    Scheduler:      python -m scripts.prune_refresh_tokens (set in Heroku Scheduler dashboard)

Environment variables:
    DATABASE_URL    Required. Standard connection string.
"""

import logging
import os
import sys

from app.env import load_environment

import psycopg2


logger = logging.getLogger(__name__)


def prune_refresh_tokens() -> int:
    """
    Deletes refresh tokens where expires_at is in the past.
    Returns the number of tokens deleted.

    No grace period: an expired refresh token has no legitimate use.
    Unlike guest accounts (where late claims are possible), expired tokens
    are definitionally dead.
    """
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM refresh_tokens
                WHERE expires_at < NOW()
                """
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
    logger.info("Pruning expired refresh tokens")


    deleted = prune_refresh_tokens()

    if deleted == 0:
        logger.info("No expired refresh tokens found")
    else:
        logger.info("Deleted %d refresh token(s)", deleted)
    
if __name__ == "__main__":
    logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prune_refresh_tokens] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    main()


