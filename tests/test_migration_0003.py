"""Schema tests for migration 0003 (game_modes.max_score, runs.claimed_tier).

Asserts the two additive nullable columns and their CHECK domains directly
against the migrated test database, mirroring test_migration_0002. Run as the
test DB owner (via DATABASE_URL), not as leaderboard_app.

Like 0002's tests, game_modes is not in conftest's TRUNCATE list, so the
`make_mode` fixture creates uniquely-named modes and tears them (and their
children) down.
"""
import os
import secrets

import psycopg
import psycopg.errors
import pytest


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
                (f"run_test_{secrets.token_hex(4)}",),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return user_id


@pytest.fixture
def make_mode(client):
    """Factory that inserts a game_mode and tears it down (with any children)."""
    created: list[str] = []

    def _make(**overrides) -> str:
        name = f"mode_{secrets.token_hex(4)}"
        cols = {"name": name}
        cols.update(overrides)
        placeholders = ", ".join(["%s"] * len(cols))
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO game_modes ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    tuple(cols.values()),
                )
                conn.commit()
        finally:
            conn.close()
        created.append(name)
        return name

    yield _make

    if created:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM scores WHERE game_mode = ANY(%s)", (created,))
                cur.execute("DELETE FROM runs WHERE game_mode = ANY(%s)", (created,))
                cur.execute(
                    "DELETE FROM submission_idempotency WHERE game_mode = ANY(%s)",
                    (created,),
                )
                cur.execute("DELETE FROM game_modes WHERE name = ANY(%s)", (created,))
                conn.commit()
        finally:
            conn.close()


def insert_run(user_id: int, game_mode: str, **overrides) -> int:
    cols = {
        "user_id": user_id,
        "game_mode": game_mode,
        "scenario_version": 1,
        "seed": 42,
        "client_run_id": secrets.token_hex(8),
        "actions": b"\x1f\x8b",  # opaque; psycopg3 adapts bytes → bytea
    }
    cols.update(overrides)
    placeholders = ", ".join(["%s"] * len(cols))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO runs ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING id",
                tuple(cols.values()),
            )
            run_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return run_id


# ── game_modes.max_score ─────────────────────────────────────────────────────

def test_max_score_defaults_null(make_mode):
    """A mode created without max_score inherits NULL (= global ceiling)."""
    name = make_mode()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max_score FROM game_modes WHERE name = %s", (name,))
            assert cur.fetchone()[0] is None
    finally:
        conn.close()


def test_max_score_is_settable_and_bigint(make_mode):
    """max_score holds values well beyond int32 (the global cap is ~1.8e11)."""
    big = 180_000_000_081
    name = make_mode(max_score=big)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max_score FROM game_modes WHERE name = %s", (name,))
            assert cur.fetchone()[0] == big
    finally:
        conn.close()


def test_max_score_check_rejects_negative(make_mode):
    with pytest.raises(psycopg.errors.CheckViolation):
        make_mode(max_score=-1)


# ── runs.claimed_tier ────────────────────────────────────────────────────────

def test_claimed_tier_defaults_null(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    run_id = insert_run(user_id, mode)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT claimed_tier FROM runs WHERE id = %s", (run_id,))
            assert cur.fetchone()[0] is None
    finally:
        conn.close()


def test_claimed_tier_is_settable(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    run_id = insert_run(user_id, mode, claimed_tier=2)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT claimed_tier FROM runs WHERE id = %s", (run_id,))
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_claimed_tier_check_rejects_out_of_range(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(user_id, mode, claimed_tier=9)
