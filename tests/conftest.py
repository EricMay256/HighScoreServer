import os
import pytest
import psycopg2
from fastapi.testclient import TestClient
from app.main import app
from app.db import _connection_pool
from unittest.mock import patch


@pytest.fixture(scope="session", autouse=True)
def configure_test_env():
    """Point the app at the test database before any tests run."""
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set — skipping integration tests")
    os.environ["DATABASE_URL"] = test_url


@pytest.fixture(scope="session")
def client(configure_test_env):
    """
    Single TestClient for the session.
    FastAPI's lifespan runs on first request, initializing the DB pool
    against TEST_DATABASE_URL.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tables():
    """
    Truncate test data between every test.
    RESTART IDENTITY resets serial sequences so IDs are predictable.
    CASCADE handles FK ordering automatically.
    """
    yield
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                TRUNCATE TABLE leaderboard_snapshots, refresh_tokens, users
                RESTART IDENTITY CASCADE
            """)
        conn.commit()
    finally:
        conn.close()

@pytest.fixture(scope="session", autouse=True)
def disable_cache():
    """
    Prevents Redis initialization during tests.
    Routes already handle a missing cache gracefully via try/except,
    so this just makes the fallback immediate rather than timeout-dependent.
    """
    with patch("app.main.init_cache"), patch("app.cache.get_cache", side_effect=RuntimeError("Cache disabled in tests")):
        yield