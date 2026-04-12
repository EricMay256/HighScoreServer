import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg2

from scripts.prune_refresh_tokens import prune_refresh_tokens


# ── Helpers ────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def insert_user() -> int:
    """Inserts a claimed user and returns their id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, is_guest)
                VALUES (%s, FALSE)
                RETURNING id
                """,
                (f"token_test_{secrets.token_hex(4)}",),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return user_id


def insert_token(user_id: int, expires_at: datetime) -> str:
    """Inserts a refresh token row. Returns the token_hash used."""
    token_hash = secrets.token_hex(16)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_hash, expires_at),
            )
            conn.commit()
    finally:
        conn.close()
    return token_hash


def token_exists(token_hash: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM refresh_tokens WHERE token_hash = %s",
                (token_hash,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


# ── Tests ──────────────────────────────────────────────────────────────────

def test_prune_deletes_expired_token(client):
    """A token with expires_at in the past should be deleted."""
    user_id = insert_user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token_hash = insert_token(user_id, past)

    deleted = prune_refresh_tokens()

    assert deleted >= 1
    assert not token_exists(token_hash)


def test_prune_spares_active_token(client):
    """A token with expires_at in the future should not be deleted."""
    user_id = insert_user()
    future = datetime.now(timezone.utc) + timedelta(days=7)
    token_hash = insert_token(user_id, future)

    prune_refresh_tokens()

    assert token_exists(token_hash)


def test_prune_deletes_multiple_expired_tokens(client):
    """Should delete all expired tokens in a single pass."""
    user_id = insert_user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    hashes = [insert_token(user_id, past) for _ in range(3)]

    deleted = prune_refresh_tokens()

    assert deleted == 3
    for token_hash in hashes:
        assert not token_exists(token_hash)


def test_prune_mixed_batch(client):
    """Expired tokens go, active tokens stay, in the same run."""
    user_id = insert_user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=7)

    expired_hashes = [insert_token(user_id, past) for _ in range(2)]
    active_hash = insert_token(user_id, future)

    deleted = prune_refresh_tokens()

    assert deleted == 2
    for token_hash in expired_hashes:
        assert not token_exists(token_hash)
    assert token_exists(active_hash)


def test_prune_returns_zero_when_nothing_expired(client):
    """Should return 0 when there's nothing to prune."""
    user_id = insert_user()
    future = datetime.now(timezone.utc) + timedelta(days=7)
    insert_token(user_id, future)

    deleted = prune_refresh_tokens()

    assert deleted == 0


def test_prune_empty_table_is_noop(client):
    """Should return 0 and not crash with no tokens at all."""
    deleted = prune_refresh_tokens()
    assert deleted == 0
