import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

from scripts.prune_idempotency_keys import prune_idempotency_keys


# ── Helpers ────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def insert_user() -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, is_guest) VALUES (%s, FALSE) RETURNING id",
                (f"idem_test_{secrets.token_hex(4)}",),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return user_id


def ensure_game_mode(name: str = "classic") -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO game_modes (name, sort_order, label)
                VALUES (%s, 'DESC', %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, name),
            )
            conn.commit()
    finally:
        conn.close()


def insert_marker(user_id: int, key: str, first_seen: datetime, game_mode: str = "classic") -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO submission_idempotency (user_id, game_mode, key, first_seen)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, game_mode, key, first_seen),
            )
            conn.commit()
    finally:
        conn.close()


def marker_exists(user_id: int, key: str, game_mode: str = "classic") -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM submission_idempotency
                WHERE user_id = %s AND game_mode = %s AND key = %s
                """,
                (user_id, game_mode, key),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


# ── Tests ──────────────────────────────────────────────────────────────────

def test_prune_deletes_old_marker(client):
    """A marker older than the retention window is deleted."""
    ensure_game_mode()
    user_id = insert_user()
    old = datetime.now(timezone.utc) - timedelta(days=31)
    insert_marker(user_id, "old-key-001", old)

    deleted = prune_idempotency_keys(prune_days=30)

    assert deleted >= 1
    assert not marker_exists(user_id, "old-key-001")


def test_prune_spares_recent_marker(client):
    """A marker within the window is kept."""
    ensure_game_mode()
    user_id = insert_user()
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    insert_marker(user_id, "recent-key-001", recent)

    prune_idempotency_keys(prune_days=30)

    assert marker_exists(user_id, "recent-key-001")


def test_prune_mixed_batch(client):
    """Old markers go, recent ones stay, in a single pass."""
    ensure_game_mode()
    user_id = insert_user()
    old = datetime.now(timezone.utc) - timedelta(days=45)
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    insert_marker(user_id, "old-a", old)
    insert_marker(user_id, "old-b", old)
    insert_marker(user_id, "recent-a", recent)

    deleted = prune_idempotency_keys(prune_days=30)

    assert deleted == 2
    assert not marker_exists(user_id, "old-a")
    assert not marker_exists(user_id, "old-b")
    assert marker_exists(user_id, "recent-a")


def test_prune_default_is_30_days(client):
    """The default retention is 30 days: a 29-day-old marker survives, 31 doesn't."""
    ensure_game_mode()
    user_id = insert_user()
    insert_marker(user_id, "day-29", datetime.now(timezone.utc) - timedelta(days=29))
    insert_marker(user_id, "day-31", datetime.now(timezone.utc) - timedelta(days=31))

    prune_idempotency_keys()  # no arg → DEFAULT_PRUNE_DAYS (30)

    assert marker_exists(user_id, "day-29")
    assert not marker_exists(user_id, "day-31")


def test_prune_empty_table_is_noop(client):
    """Returns 0 and doesn't crash with nothing to prune."""
    assert prune_idempotency_keys(prune_days=30) == 0
