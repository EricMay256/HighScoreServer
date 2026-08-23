from unittest.mock import AsyncMock, patch

import pytest

import app.db


@pytest.fixture(autouse=True)
def session_pool_survives():
    """`app.db._pool` is module-level state the whole session shares.

    These tests drive `init_db`/`close_db` directly, and both write it. The
    session `TestClient` holds no reference of its own -- `get_pool()` reads the
    module attribute on every request -- so a test that leaves it None does not
    fail here, it fails in whichever API test happens to run next, with
    "Connection pool not initialized" and a 500 on code that is perfectly
    correct.

    That made the suite order-dependent rather than broken: leaderboard tests
    currently run first, so the default order hides it. Selecting, shuffling, or
    appending tests reveals it.

    Asserts *and* restores. Restoring alone would make a future leak harmless
    and invisible; asserting alone would leave the rest of the session broken on
    the day it fires.
    """

    before = app.db._pool
    try:
        yield
    finally:
        after = app.db._pool
        app.db._pool = before
        assert after is before, (
            "a test left app.db._pool replaced; the session pool it clobbered "
            "is what every later API test uses"
        )


@pytest.mark.asyncio
async def test_hss_pool_defaults_to_essential_zero_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detaches the session's real pool from this test before `init_db` writes
    # over it, so monkeypatch's teardown puts the original back.
    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret@db.example.test:5432/hss",
    )
    monkeypatch.delenv("HSS_DB_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("HSS_DB_POOL_MAX_SIZE", raising=False)
    pool = AsyncMock()

    with patch("app.db.AsyncConnectionPool", return_value=pool) as pool_type:
        await app.db.init_db()

    pool_type.assert_called_once_with(
        conninfo="postgresql://user:secret@db.example.test:5432/hss",
        min_size=1,
        max_size=4,
        open=False,
    )
    pool.open.assert_awaited_once_with()
    await app.db.close_db()


@pytest.mark.asyncio
async def test_hss_pool_rejects_inverted_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret@db.example.test:5432/hss",
    )
    monkeypatch.setenv("HSS_DB_POOL_MIN_SIZE", "6")
    monkeypatch.setenv("HSS_DB_POOL_MAX_SIZE", "5")

    with pytest.raises(RuntimeError, match="cannot exceed"):
        await app.db.init_db()
