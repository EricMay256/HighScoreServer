import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.vault.settings import vault_enabled
from tests.vault.test_search import clear_corpus, seed_corpus, vault_service


READ_KEY = "test-vault-read-key"

pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)


@pytest.fixture
def seeded_corpus(configure_test_env: None) -> dict[str, str]:
    """Seed the vault corpus for the duration of one test."""

    service, engine = vault_service()
    ids = asyncio.run(seed_corpus(service, uuid4().hex))
    try:
        yield ids
    finally:
        asyncio.run(clear_corpus(service, ids))
        asyncio.run(engine.dispose())


def test_search_requires_credentials(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get("/api/vault/search", params={"q": "running"})

    assert response.status_code == 401


def test_search_rejects_a_wrong_key(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/search",
        params={"q": "running"},
        headers={"Authorization": "Bearer not-the-key"},
    )

    assert response.status_code == 401


def test_search_refuses_to_serve_when_no_key_is_configured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unset secret must close the door, never open it.
    monkeypatch.delenv("VAULT_READ_API_KEY", raising=False)

    response = client.get(
        "/api/vault/search",
        params={"q": "running"},
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 503


def test_search_returns_lexical_hits(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    seeded_corpus: dict[str, str],
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/search",
        params={"q": "running", "limit": 5},
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "running"
    # No embedding credential is configured in the test environment. The
    # response must name that as the reason rather than reporting a bare
    # "vector search didn't run", which would look identical to an outage.
    assert body["vector_status"] == "not_configured"
    assert body["profile_id"] is None
    assert [hit["note_id"] for hit in body["hits"]] == [seeded_corpus["alpha"]]
    assert body["hits"][0]["lexical_rank"] == 1


def test_search_validates_its_query_parameters(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)
    headers = {"Authorization": f"Bearer {READ_KEY}"}

    assert client.get("/api/vault/search", headers=headers).status_code == 422
    assert (
        client.get(
            "/api/vault/search",
            params={"q": "x", "limit": 0},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/vault/search",
            params={"q": "x", "limit": 500},
            headers=headers,
        ).status_code
        == 422
    )


def test_search_rejects_a_whitespace_only_query(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # min_length=1 admits " ". The embedding port raises ValueError on a blank
    # input, which is not an EmbeddingError and so escapes the service's
    # lexical-fallback path — a 500 for a request that is simply invalid. No
    # embedding provider is configured here, so this asserts the boundary
    # check rather than the failure it prevents downstream.
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/search",
        params={"q": "   "},
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 422


def test_search_strips_surrounding_whitespace(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    seeded_corpus: dict[str, str],
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/search",
        params={"q": "  running  ", "limit": 5},
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 200
    body = response.json()
    # The response echoes what was searched, not what was typed.
    assert body["query"] == "running"
    assert [hit["note_id"] for hit in body["hits"]] == [seeded_corpus["alpha"]]


def test_search_rejects_a_non_ascii_key_without_erroring(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # hmac.compare_digest refuses a str holding non-ASCII. The bearer token is
    # attacker-controlled and Starlette decodes headers as latin-1, so a raw
    # high byte must produce a clean 401 rather than an unhandled TypeError.
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/search",
        params={"q": "running"},
        headers={b"Authorization": b"Bearer n\xf6pe"},
    )

    assert response.status_code == 401


def test_document_fetch_rejects_a_non_ascii_key_without_erroring(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/documents/anything",
        headers={b"Authorization": b"Bearer n\xf6pe"},
    )

    assert response.status_code == 401


def test_document_is_fetchable_by_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    seeded_corpus: dict[str, str],
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        f"/api/vault/documents/{seeded_corpus['alpha']}",
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["note_id"] == seeded_corpus["alpha"]
    assert body["title"] == "Postgres indexing"
    assert body["status"] == "active"
    # The read surface does not carry canonical_url; that belongs to the write
    # path's response contract.
    assert "canonical_url" not in body


def test_archived_document_is_still_resolvable_by_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    seeded_corpus: dict[str, str],
) -> None:
    # Search excludes it, but a related_ids or source_ids reference pointing at
    # retired history should resolve rather than dead-end.
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        f"/api/vault/documents/{seeded_corpus['gamma']}",
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_flagged_document_is_not_served_by_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    seeded_corpus: dict[str, str],
) -> None:
    # "flagged" means the write path's policy declined to endorse the content.
    # The read surface withholds it rather than handing it to an agent that
    # will not think to check the status field.
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        f"/api/vault/documents/{seeded_corpus['delta']}",
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 404


def test_unknown_document_is_a_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get(
        "/api/vault/documents/does-not-exist",
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )

    assert response.status_code == 404


def test_document_fetch_requires_credentials(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_READ_API_KEY", READ_KEY)

    response = client.get("/api/vault/documents/anything")

    assert response.status_code == 401
