from unittest.mock import AsyncMock, patch

import pytest

import app.db


@pytest.mark.asyncio
async def test_hss_pool_defaults_to_essential_zero_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        max_size=5,
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
