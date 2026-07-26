from collections.abc import Iterator
import os

import pytest

from tests.vault.migration_helpers import create_database, drop_database


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
