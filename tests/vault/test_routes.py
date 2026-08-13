import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy import delete as sql_delete

from app.vault.auth import TOKEN_PREFIX, VaultScope, hash_secret
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.rate_limit import LIMITS
from app.vault.repository import VaultDocumentRepository
from app.vault.settings import vault_enabled
from app.vault.tables import vault_agent_credentials, vault_documents
from tests.vault.test_search import clear_corpus, seed_corpus, vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)


def _issue(scopes: tuple[str, ...] = (VaultScope.READ,), **overrides) -> tuple[str, str]:
    """Insert a credential and return (credential_id, bearer token)."""

    credential_id = uuid4().hex[:16]
    secret = uuid4().hex + uuid4().hex
    service, engine = vault_service()

    async def create() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                insert(vault_agent_credentials).values(
                    id=credential_id,
                    principal_id=f"test-principal-{credential_id}",
                    display_name="test credential",
                    secret_sha256=hash_secret(secret),
                    scopes=list(scopes),
                    **overrides,
                )
            )

    try:
        asyncio.run(create())
    finally:
        asyncio.run(engine.dispose())
    return credential_id, f"{TOKEN_PREFIX}_{credential_id}_{secret}"


def _drop(credential_id: str) -> None:
    service, engine = vault_service()

    async def remove() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                sql_delete(vault_agent_credentials).where(
                    vault_agent_credentials.c.id == credential_id
                )
            )

    try:
        asyncio.run(remove())
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture
def read_token(configure_test_env: None) -> str:
    """A credential granting vault:read, removed after the test."""

    credential_id, token = _issue()
    try:
        yield token
    finally:
        _drop(credential_id)


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
) -> None:

    response = client.get("/api/v1/vault/search", params={"q": "running"})

    assert response.status_code == 401


def test_search_rejects_a_wrong_key(
    client: TestClient,
) -> None:

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "running"},
        headers={"Authorization": "Bearer not-the-key"},
    )

    assert response.status_code == 401


def test_search_rejects_an_unknown_credential(
    client: TestClient,
) -> None:
    """A well-formed token naming no credential is refused.

    Replaces the old "no shared key configured -> 503" case: there is no
    global switch any more, so the only states are a credential that verifies
    and one that does not.
    """

    absent = f"{TOKEN_PREFIX}_{uuid4().hex[:16]}_{uuid4().hex + uuid4().hex}"

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "running"},
        headers={"Authorization": f"Bearer {absent}"},
    )

    assert response.status_code == 401


def test_search_rejects_a_credential_without_the_read_scope(
    client: TestClient,
) -> None:
    """Authenticated but unauthorized is 403, not 401.

    The distinction matters to an operator: a bad token is a client that
    cannot talk to us, a missing scope is one we deliberately did not grant
    something.
    """

    credential_id, token = _issue(scopes=(VaultScope.EXPORT,))
    try:
        response = client.get(
            "/api/v1/vault/search",
            params={"q": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 403


def test_search_rejects_a_revoked_credential(
    client: TestClient,
) -> None:
    """Revocation takes effect on the next request, with no cache to expire."""

    credential_id, token = _issue(revoked_at=datetime(2020, 1, 1, tzinfo=UTC))
    try:
        response = client.get(
            "/api/v1/vault/search",
            params={"q": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 401


def test_search_rejects_an_expired_credential(
    client: TestClient,
) -> None:
    credential_id, token = _issue(expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    try:
        response = client.get(
            "/api/v1/vault/search",
            params={"q": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 401


def test_search_returns_lexical_hits(
    client: TestClient,
    read_token: str,
    seeded_corpus: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patched to None rather than relying on VAULT_EMBEDDING_API_KEY being
    # absent from the environment. Once a real key landed in .env this test
    # began exercising the `used` path and failing on an assertion about
    # `not_configured` — an ambient dependency, not a behaviour change. Same
    # mechanism test_contributions.py uses to inject its stub.
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: None)

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "running", "limit": 5},
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "running"
    # With no embedding provider configured, the response must name that as the
    # reason rather than reporting a bare "vector search didn't run", which
    # would look identical to an outage.
    assert body["vector_status"] == "not_configured"
    assert body["profile_id"] is None
    assert [hit["note_id"] for hit in body["hits"]] == [seeded_corpus["alpha"]]
    assert body["hits"][0]["lexical_rank"] == 1


def test_search_validates_its_query_parameters(
    client: TestClient,
    read_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {read_token}"}

    assert client.get("/api/v1/vault/search", headers=headers).status_code == 422
    assert (
        client.get(
            "/api/v1/vault/search",
            params={"q": "x", "limit": 0},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/vault/search",
            params={"q": "x", "limit": 500},
            headers=headers,
        ).status_code
        == 422
    )


def test_search_rejects_a_whitespace_only_query(
    client: TestClient,
    read_token: str,
) -> None:
    # min_length=1 admits " ". The embedding port raises ValueError on a blank
    # input, which is not an EmbeddingError and so escapes the service's
    # lexical-fallback path — a 500 for a request that is simply invalid. No
    # embedding provider is configured here, so this asserts the boundary
    # check rather than the failure it prevents downstream.

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "   "},
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 422


def test_search_strips_surrounding_whitespace(
    client: TestClient,
    read_token: str,
    seeded_corpus: dict[str, str],
) -> None:

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "  running  ", "limit": 5},
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    # The response echoes what was searched, not what was typed.
    assert body["query"] == "running"
    assert [hit["note_id"] for hit in body["hits"]] == [seeded_corpus["alpha"]]


def test_search_rejects_a_non_ascii_key_without_erroring(
    client: TestClient,
) -> None:
    # hmac.compare_digest refuses a str holding non-ASCII. The bearer token is
    # attacker-controlled and Starlette decodes headers as latin-1, so a raw
    # high byte must produce a clean 401 rather than an unhandled TypeError.

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "running"},
        headers={b"Authorization": b"Bearer n\xf6pe"},
    )

    assert response.status_code == 401


def test_document_fetch_rejects_a_non_ascii_key_without_erroring(
    client: TestClient,
) -> None:

    response = client.get(
        "/api/v1/vault/notes/anything",
        headers={b"Authorization": b"Bearer n\xf6pe"},
    )

    assert response.status_code == 401


def test_document_is_fetchable_by_id(
    client: TestClient,
    read_token: str,
    seeded_corpus: dict[str, str],
) -> None:

    response = client.get(
        f"/api/v1/vault/notes/{seeded_corpus['alpha']}",
        headers={"Authorization": f"Bearer {read_token}"},
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
    read_token: str,
    seeded_corpus: dict[str, str],
) -> None:
    # Search excludes it, but a related_ids or source_ids reference pointing at
    # retired history should resolve rather than dead-end.

    response = client.get(
        f"/api/v1/vault/notes/{seeded_corpus['gamma']}",
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_flagged_document_is_not_served_by_id(
    client: TestClient,
    read_token: str,
    seeded_corpus: dict[str, str],
) -> None:
    # "flagged" means the write path's policy declined to endorse the content.
    # The read surface withholds it rather than handing it to an agent that
    # will not think to check the status field.

    response = client.get(
        f"/api/v1/vault/notes/{seeded_corpus['delta']}",
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 404


def test_unknown_document_is_a_404(
    client: TestClient,
    read_token: str,
) -> None:

    response = client.get(
        "/api/v1/vault/notes/does-not-exist",
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 404


def test_document_fetch_requires_credentials(
    client: TestClient,
) -> None:

    response = client.get("/api/v1/vault/notes/anything")

    assert response.status_code == 401


def test_read_surface_carries_doc_type_including_when_untyped(
    client: TestClient,
    read_token: str,
    configure_test_env: None,
) -> None:
    """The Type Dictionary value reaches the agent, and null is explicit.

    The consumer is an agent choosing what to read, so the type has to be on
    the response rather than inferrable only from the body. An untyped
    document reports null instead of omitting the field, so "untyped" and
    "this deployment predates doc_type" do not look the same.
    """

    service, engine = vault_service()
    documents = VaultDocumentRepository()
    typed_id = f"route-doctype-{uuid4().hex}"
    untyped_id = f"route-doctype-{uuid4().hex}"

    async def seed() -> None:
        async with service.transaction() as connection:
            for document_id, doc_type, doc_status in (
                (typed_id, "Decision", "Accepted"),
                (untyped_id, None, None),
            ):
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.NOTE,
                        vault_path=f"Agent/notes/{document_id}.md",
                        doc_type=doc_type,
                        status=DocumentStatus.ACTIVE,
                        doc_status=doc_status,
                        title="Type Dictionary fixture",
                        body="Exercises doc_type on the read surface.",
                        contributed_by="test:doc-type",
                        provenance={"fixture": True},
                    ),
                )

    async def clear() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                delete(vault_documents).where(
                    vault_documents.c.id.in_([typed_id, untyped_id])
                )
            )

    asyncio.run(seed())
    try:
        typed = client.get(
            f"/api/v1/vault/notes/{typed_id}",
            headers={"Authorization": f"Bearer {read_token}"},
        )
        untyped = client.get(
            f"/api/v1/vault/notes/{untyped_id}",
            headers={"Authorization": f"Bearer {read_token}"},
        )
    finally:
        asyncio.run(clear())
        asyncio.run(engine.dispose())

    assert typed.status_code == 200
    typed_body = typed.json()
    assert typed_body["doc_type"] == "Decision"
    # doc_status is the governance lifecycle; status is the vault's own
    # visibility state. Both reach the caller, separately.
    assert typed_body["doc_status"] == "Accepted"
    assert typed_body["status"] == "active"
    assert typed_body["vault_path"] == f"Agent/notes/{typed_id}.md"

    assert untyped.status_code == 200
    untyped_body = untyped.json()
    for field in ("doc_type", "doc_status"):
        assert field in untyped_body
        assert untyped_body[field] is None
    # vault_path is NOT NULL, so it is present even on an otherwise bare row.
    assert untyped_body["vault_path"] == f"Agent/notes/{untyped_id}.md"


def test_search_returns_429_with_retry_after_once_the_burst_is_spent(
    client: TestClient,
    read_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quota is per principal, and the response says when to come back.

    A fresh credential means a fresh bucket, so this does not depend on what
    other tests spent.

    The clock is frozen because the bucket refills continuously: search allows
    30/min, so 0.5 tokens arrive per second, and spending a burst of 10 takes
    over two seconds whenever the database is slow — which refills a token and
    lets the request that should be refused through. That made this pass alone
    and fail in a full run. Freezing time tests the quota rather than the speed
    of the suite.
    """

    monkeypatch.setattr("app.vault.rate_limit.time.monotonic", lambda: 1_000.0)

    headers = {"Authorization": f"Bearer {read_token}"}
    limit = LIMITS["search"]

    for _ in range(limit.burst):
        allowed = client.get(
            "/api/v1/vault/search", params={"q": "running"}, headers=headers
        )
        assert allowed.status_code == 200

    refused = client.get(
        "/api/v1/vault/search", params={"q": "running"}, headers=headers
    )

    assert refused.status_code == 429
    # Integer seconds, and never 0 — a 0 invites an immediate retry that is
    # refused again.
    assert int(refused.headers["Retry-After"]) >= 1


def test_the_quota_is_charged_per_operation_not_per_credential(
    client: TestClient,
    read_token: str,
) -> None:
    """Exhausting search must not lock the caller out of fetching notes."""

    headers = {"Authorization": f"Bearer {read_token}"}

    for _ in range(LIMITS["search"].burst + 1):
        client.get("/api/v1/vault/search", params={"q": "running"}, headers=headers)

    # Search is spent; get_note has its own, larger bucket. 404 rather than
    # 429 is the point — the request was allowed through.
    response = client.get("/api/v1/vault/notes/does-not-exist", headers=headers)

    assert response.status_code == 404
