from scripts.prune_guests import prune_guests
from datetime import datetime, timezone, timedelta
import psycopg2
import os


# ── Helpers ────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def insert_guest(created_at: datetime) -> int:
    """Inserts a guest account with a specific created_at. Returns user_id."""
    import secrets
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, is_guest, created_at)
                VALUES (%s, TRUE, %s)
                RETURNING id
                """,
                (f"guest_{secrets.token_hex(4)}", created_at),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return user_id


def user_exists(user_id: int) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def insert_score(user_id: int, game_mode: str = "classic") -> None:
    """Inserts a score row for the given user."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scores
                    (user_id, game_mode, score, period, period_start, submitted_at)
                VALUES (%s, %s, 100, 'alltime', '2000-01-01 00:00:00+00', NOW())
                """,
                (user_id, game_mode),
            )
            conn.commit()
    finally:
        conn.close()


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


# ── Tests ──────────────────────────────────────────────────────────────────

def test_prune_deletes_old_scoreless_guest(client):
    """A guest with no scores older than the threshold should be deleted."""
    old = datetime.now(timezone.utc) - timedelta(days=31)
    user_id = insert_guest(old)

    deleted = prune_guests(prune_days=30)

    assert deleted >= 1
    assert not user_exists(user_id)


def test_prune_spares_recent_guest(client):
    """A guest created within the threshold window should not be deleted."""
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    user_id = insert_guest(recent)

    prune_guests(prune_days=30)

    assert user_exists(user_id)


def test_prune_spares_guest_with_scores(client):
    """A guest older than the threshold but with scores should not be deleted."""
    ensure_game_mode()
    old = datetime.now(timezone.utc) - timedelta(days=31)
    user_id = insert_guest(old)
    insert_score(user_id)

    prune_guests(prune_days=30)

    assert user_exists(user_id)


def test_prune_spares_claimed_accounts(client):
    """Claimed accounts should never be pruned regardless of age or scores."""
    conn = get_conn()
    old = datetime.now(timezone.utc) - timedelta(days=31)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, is_guest, created_at)
                VALUES (%s, FALSE, %s)
                RETURNING id
                """,
                ("claimed_old_user", old),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()

    prune_guests(prune_days=30)

    assert user_exists(user_id)


def test_prune_returns_zero_when_nothing_eligible(client):
    """Should return 0 and not crash when there's nothing to prune."""
    deleted = prune_guests(prune_days=30)
    assert deleted == 0


def test_prune_deletes_multiple_eligible_guests(client):
    """Should delete all eligible guests in a single pass."""
    old = datetime.now(timezone.utc) - timedelta(days=31)
    ids = [insert_guest(old) for _ in range(3)]

    deleted = prune_guests(prune_days=30)

    assert deleted == 3
    for user_id in ids:
        assert not user_exists(user_id)