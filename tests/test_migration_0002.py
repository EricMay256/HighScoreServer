"""Schema tests for migration 0002 (runs, cumulative scoring, validated runs).

Phase 1 is migration-only — there are no endpoints yet — so these assert the
schema *guarantees* directly against the migrated test database: additive
defaults, the new CHECK domains, the anti-replay uniqueness, the dedup
ON CONFLICT behavior, and the scores->runs link. They run as the test DB owner
(via DATABASE_URL), not as leaderboard_app; the grant layer is verified
separately by re-applying db/role.sql.

game_modes is intentionally NOT in conftest's TRUNCATE list (seed modes must
survive between tests), so every test here creates uniquely-named modes and the
`make_mode` fixture deletes them — and any rows that reference them — on
teardown.
"""
import os
import secrets

import psycopg
import psycopg.errors
import pytest


# ── Helpers / fixtures ───────────────────────────────────────────────────────

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
                # Clear children first; this is order-independent of conftest's
                # autouse truncate so teardown never trips an FK.
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
        "actions": b"\x1f\x8b",  # not real gzip; opaque here (psycopg3 adapts bytes → bytea)
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


# ── game_modes additive columns ──────────────────────────────────────────────

def test_game_modes_additive_defaults(make_mode):
    """A mode created with only a name gets the safe additive defaults."""
    name = make_mode()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT required_tier, scoring_strategy, game_key "
                "FROM game_modes WHERE name = %s",
                (name,),
            )
            required_tier, scoring_strategy, game_key = cur.fetchone()
    finally:
        conn.close()
    assert required_tier == 0
    assert scoring_strategy == "best"
    assert game_key is None


def test_scoring_strategy_check_rejects_unknown(make_mode):
    with pytest.raises(psycopg.errors.CheckViolation):
        make_mode(scoring_strategy="averaged")


def test_required_tier_check_rejects_out_of_range(make_mode):
    with pytest.raises(psycopg.errors.CheckViolation):
        make_mode(required_tier=4)


def test_game_key_is_settable(make_mode):
    name = make_mode(game_key="flick_fest")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT game_key FROM game_modes WHERE name = %s", (name,))
            assert cur.fetchone()[0] == "flick_fest"
    finally:
        conn.close()


# ── runs ─────────────────────────────────────────────────────────────────────

def test_runs_status_defaults_pending(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    run_id = insert_run(user_id, mode)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, canonical_score, validation_tier "
                "FROM runs WHERE id = %s",
                (run_id,),
            )
            status, canonical, tier = cur.fetchone()
    finally:
        conn.close()
    assert status == "pending"
    assert canonical is None
    assert tier is None


def test_runs_client_run_id_unique_per_user_mode(make_mode):
    """Anti-replay: a repeated (user, mode, client_run_id) is rejected."""
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    dup = "replay_abc123"
    insert_run(user_id, mode, client_run_id=dup)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_run(user_id, mode, client_run_id=dup)


def test_runs_status_check_rejects_unknown(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(user_id, mode, status="approved")


def test_runs_validation_tier_check_rejects_out_of_range(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(user_id, mode, validation_tier=9)


# ── submission_idempotency ───────────────────────────────────────────────────

def test_idempotency_on_conflict_do_nothing_dedups(make_mode):
    """Second insert of the same key is a no-op (rowcount 0)."""
    mode = make_mode(scoring_strategy="cumulative")
    user_id = insert_user()
    key = "idem_key_xyz"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submission_idempotency (user_id, game_mode, key) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, mode, key),
            )
            first = cur.rowcount
            cur.execute(
                "INSERT INTO submission_idempotency (user_id, game_mode, key) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, mode, key),
            )
            second = cur.rowcount
            conn.commit()
    finally:
        conn.close()
    assert first == 1
    assert second == 0


# ── scores.run_id link ───────────────────────────────────────────────────────

def test_scores_run_id_links_to_run(make_mode):
    mode = make_mode(required_tier=1)
    user_id = insert_user()
    run_id = insert_run(user_id, mode)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scores
                    (user_id, game_mode, score, period, period_start, run_id)
                VALUES (%s, %s, %s, 'alltime', NOW(), %s)
                RETURNING run_id
                """,
                (user_id, mode, 100, run_id),
            )
            assert cur.fetchone()[0] == run_id
            conn.commit()
    finally:
        conn.close()


def test_scores_run_id_nullable_for_raw(make_mode):
    """Raw submissions still insert with no run_id (backward compatible)."""
    mode = make_mode()
    user_id = insert_user()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scores
                    (user_id, game_mode, score, period, period_start)
                VALUES (%s, %s, %s, 'alltime', NOW())
                RETURNING run_id
                """,
                (user_id, mode, 100),
            )
            assert cur.fetchone()[0] is None
            conn.commit()
    finally:
        conn.close()
