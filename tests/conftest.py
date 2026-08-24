import asyncio
import os
import sys


# psycopg3's async pool cannot run on Windows' default ProactorEventLoop — it
# drives sockets with loop.add_reader/add_writer, which only SelectorEventLoop
# implements. Force the Selector policy before any event loop is created so the
# TestClient's anyio portal and pytest-asyncio both pick it up. No effect on
# Linux/CI (Heroku), where SelectorEventLoop is already the default.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from unittest.mock import patch

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient


load_dotenv()  # load .env before any os.environ reads
os.environ["RATE_LIMITER_ENABLED"] = "false"  # disable rate limiter for tests
# The vault's OAuth authorization server is assembled inside create_app() from
# this variable -- its presence is the on/off switch (vault ADR 0024) -- and
# `app` below is built at import. Setting it here is what puts /authorize,
# /token and /vault/login on the one application the whole suite shares.
#
# Sharing matters more than it looks. A test that built its own app and entered
# its lifespan would tear the vault engine down for every other test:
# close_vault_db() sets the module-level engine back to None, and the pooled
# connections belong to the session client's event loop. Both failures are
# action-at-a-distance in files the offending test never mentions.
os.environ.setdefault("VAULT_PUBLIC_URL", "https://vault.test.invalid")
from app.main import app  # noqa: E402 Must import after overriding env var


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
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                TRUNCATE TABLE submission_idempotency, scores, runs,
                               refresh_tokens, auth_identities, users
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
