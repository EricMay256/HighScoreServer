import os
from collections.abc import Iterator

import pytest

from app.vault.rate_limit import reset_ip_limiter
from tests.vault.migration_helpers import create_database, drop_database


@pytest.fixture(autouse=True)
def clear_preauth_ip_buckets() -> Iterator[None]:
    """Give every vault test a fresh pre-auth bucket.

    The per-principal quota needs no equivalent: a test that issues its own
    credential gets a clean bucket for free. The pre-auth guard is keyed on the
    client address, and every test shares a loopback one, so without this the
    suite's own request volume accumulates into one bucket and tests start
    failing according to the order they ran in.
    """

    reset_ip_limiter()
    yield
    reset_ip_limiter()


@pytest.fixture(scope="session")
def disposable_database_urls(
    configure_test_env: None,
) -> Iterator[dict[str, str]]:
    base_url = os.environ["DATABASE_URL"]
    created: list[tuple[str, str]] = []
    urls: dict[str, str] = {}
    try:
        for role in ("shared", "leaderboard", "vault"):
            name, url = create_database(base_url, f"hss_vault_{role}")
            created.append((name, url))
            urls[role] = url
        yield urls
    finally:
        for name, _url in reversed(created):
            drop_database(base_url, name)
