import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture(scope="session")
def vault_client(composed_client: TestClient) -> TestClient:
    """A client whose requests reach the vault application alone (host ADR 0016).

    It targets the very vault app inside the composed one -- the instance whose
    lifespan started the engine and the MCP session manager -- and runs on
    ``composed_client``'s event loop by sharing its portal. Starting a second
    lifespan instead would fail outright (``init_vault_db`` refuses a second
    engine), and a client with its own loop would inherit a pool bound to the
    wrong one. ``TestClient.portal`` is public, and ``_portal_factory`` reuses it
    when set; that is the whole mechanism.

    After extraction this fixture owns the lifespan itself, and nothing that
    uses it changes.
    """

    composed = composed_client.app
    vault_app = getattr(composed, "vault", None)
    if vault_app is None:
        pytest.skip("VAULT_ENABLED is off, so there is no vault application")
    vault = TestClient(vault_app)
    vault.portal = composed_client.portal
    return vault
