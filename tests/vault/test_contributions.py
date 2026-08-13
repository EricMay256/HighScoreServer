"""The governed write path, end to end over HTTP.

These are the first route tests in the suite that configure an embedding
provider. Every earlier route test runs the `not_configured` path, which is how
a blank-query 500 stayed invisible through a whole review — so the stub below
exists to make the *configured* path reachable, not merely to satisfy a
constructor.

The stub is deterministic in text: identical text yields an identical vector.
That is what makes the dedup assertions meaningful rather than incidental.
"""

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.vault.api_models import VaultContributionRequest
from app.vault.auth import VaultScope  # noqa: E402  (grouped with vault imports)
from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.embeddings import EmbeddingInputKind, EmbeddingVector
from app.vault.routes import _canonical_request_digest
from app.vault.service import REQUEST_DIGEST_VERSION
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_audit_events,
    vault_documents,
    vault_review_cases,
    vault_write_requests,
)
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PROFILE_ID = "test/contrib-model:1536"


class StubEmbeddingProvider:
    """Deterministic embeddings derived from the text itself.

    Same text -> same vector -> cosine similarity 1.0, which is what the
    shipped policy flags on. Different text -> a different direction, which
    keeps unrelated notes below any band.
    """

    profile_id = PROFILE_ID
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls = 0

    async def embed(
        self, texts, kind: EmbeddingInputKind
    ) -> tuple[EmbeddingVector, ...]:
        del kind
        self.calls += 1
        return tuple(self._vector(text) for text in texts)

    @staticmethod
    def _vector(text: str) -> EmbeddingVector:
        digest = sha256(text.encode("utf-8")).digest()
        # A sparse one-hot-ish vector: identical text lands on the same axis,
        # different text almost never collides, so cosine is ~1.0 or ~0.0.
        axis = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        return tuple(
            1.0 if index == axis else 0.0 for index in range(EMBEDDING_DIMENSIONS)
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> StubEmbeddingProvider:
    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: stub)
    return stub


@pytest.fixture
def write_token(configure_test_env: None) -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        yield token
    finally:
        _drop(credential_id)


def _cleanup(principal_prefix: str = "test-principal-") -> None:
    service, engine = vault_service()

    async def remove() -> None:
        # Order matters: vault_write_requests.document_id and
        # vault_review_cases.candidate_document_id both reference
        # vault_documents, so the referencing rows go first. Neither FK
        # cascades, deliberately -- an audit trail that vanished with its
        # subject would not be an audit trail.
        async with service.transaction() as connection:
            await connection.execute(
                delete(vault_write_requests).where(
                    vault_write_requests.c.principal_id.like(f"{principal_prefix}%")
                )
            )
            await connection.execute(
                delete(vault_audit_events).where(
                    vault_audit_events.c.principal_id.like(f"{principal_prefix}%")
                )
            )
            # Swept by contributor rather than by collected id. A test that
            # fails partway through never returns its ids, and a stray active
            # document perturbs the dedup query for every later test -- so the
            # sweep has to catch what the caller could not name.
            contributed_by = f"agent:{principal_prefix}%"
            orphans = (
                select(vault_documents.c.id)
                .where(vault_documents.c.contributed_by.like(contributed_by))
                .scalar_subquery()
            )
            await connection.execute(
                delete(vault_review_cases).where(
                    vault_review_cases.c.candidate_document_id.in_(orphans)
                )
            )
            await connection.execute(
                delete(vault_documents).where(
                    vault_documents.c.contributed_by.like(contributed_by)
                )
            )

    try:
        asyncio.run(remove())
    finally:
        asyncio.run(engine.dispose())


def _payload(**overrides) -> dict:
    base = {
        "title": f"Contribution {uuid4().hex[:8]}",
        "body": "A note body distinctive enough not to collide.",
        "tags": ["testing"],
        "idempotency_key": f"key-{uuid4().hex}",
    }
    return {**base, **overrides}


def test_a_contribution_is_inserted_and_becomes_retrievable(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=body, headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["status"] == "inserted"
        assert payload["idempotent_replay"] is False

        # The write path assigns identity and path; the caller does not.
        fetched = client.get(
            f"/api/v1/vault/notes/{payload['note_id']}", headers=headers
        )
        assert fetched.status_code == 200
        detail = fetched.json()
        assert detail["title"] == body["title"]
        assert detail["vault_path"] == f"Agent/notes/{payload['note_id']}.md"
        # types.yml constrains Agent/notes/** to exactly this type.
        assert detail["doc_type"] == "Agent Note"
        assert detail["status"] == "active"
    finally:
        _cleanup()


def test_replaying_an_idempotency_key_does_not_write_twice(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """A retry must be a no-op, not a second note that flags as its own dupe."""

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text
        calls_after_first = provider.calls

        second = client.post("/api/v1/vault/contributions", json=body, headers=headers)

        assert second.status_code == 200
        assert second.json()["note_id"] == first.json()["note_id"]
        assert second.json()["idempotent_replay"] is True
        # A replay must not buy an embedding call.
        assert provider.calls == calls_after_first
    finally:
        _cleanup()


def test_reusing_a_key_for_a_different_body_is_a_conflict(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        conflicting = {**body, "body": "Entirely different content."}
        response = client.post(
            "/api/v1/vault/contributions", json=conflicting, headers=headers
        )

        assert response.status_code == 409
    finally:
        _cleanup()


def test_key_order_and_whitespace_do_not_make_a_conflict(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The digest covers the validated model, not the raw bytes.

    Two JSON documents differing only in key order are the same request, and
    refusing the retry would be wrong.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()
    reordered = dict(reversed(list(body.items())))

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        second = client.post(
            "/api/v1/vault/contributions", json=reordered, headers=headers
        )

        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True
    finally:
        _cleanup()


def test_the_digest_ignores_fields_the_caller_did_not_send() -> None:
    """Adding an optional field must not change existing requests' digests.

    This is the regression test for the defect migration 0006 records: the
    digest hashed the validated model *including* unset fields at their
    defaults, so `5bdd5ad` adding `summary`, `aliases`, `facets`, `related_ids`
    and `source_ids` changed the digest of every request that had ever been
    made. 48 imported notes replayed as 409s with identical bytes on the wire.

    Asserting on the canonical form rather than on a frozen hash keeps the test
    honest about *why*: a future optional field cannot appear here unless a
    caller sent it.
    """

    supplied = {
        "title": "A title",
        "body": "A body.",
        "tags": ["testing"],
        "idempotency_key": f"key-{uuid4().hex}",
    }
    model = VaultContributionRequest.model_validate(supplied)
    canonical = model.model_dump_json(exclude_unset=True)

    assert set(json.loads(canonical)) == set(supplied)
    for unsent in ("summary", "aliases", "facets", "related_ids", "source_ids"):
        assert unsent not in canonical

    assert (
        _canonical_request_digest(model) == sha256(canonical.encode("utf-8")).digest()
    )


def _force_stored_digest(
    principal_prefix: str, *, digest_version: int, request_sha256: bytes
) -> None:
    """Rewrite the stored digest of every write request a test just made."""

    service, engine = vault_service()

    async def rewrite() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                update(vault_write_requests)
                .where(vault_write_requests.c.principal_id.like(f"{principal_prefix}%"))
                .values(
                    digest_version=digest_version,
                    request_sha256=request_sha256,
                )
            )

    try:
        asyncio.run(rewrite())
    finally:
        asyncio.run(engine.dispose())


def _stored_digests(principal_prefix: str) -> list[tuple[int, bytes]]:
    """Read back (digest_version, request_sha256) for a test's write requests."""

    service, engine = vault_service()

    async def read() -> list[tuple[int, bytes]]:
        async with service.transaction() as connection:
            result = await connection.execute(
                select(
                    vault_write_requests.c.digest_version,
                    vault_write_requests.c.request_sha256,
                ).where(
                    vault_write_requests.c.principal_id.like(f"{principal_prefix}%")
                )
            )
            return [(row[0], bytes(row[1])) for row in result.all()]

    try:
        return asyncio.run(read())
    finally:
        asyncio.run(engine.dispose())


def test_a_digest_from_a_retired_rule_replays_and_is_restated(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """A stored digest is only evidence when both sides used the same rule.

    Pre-0006 rows cannot be recompared -- only the digest was stored, never the
    payload -- so a mismatch there is an absence of evidence rather than proof
    of a different request. Refusing would strand those keys on a 409 that no
    caller could ever clear.

    The replay restates the digest under the current rule, so the concession
    lasts one call rather than forever. Without that the row stays uncomparable
    for the rest of its life and conflict detection never comes back.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text
        calls_after_first = provider.calls

        _force_stored_digest(
            "test-principal-", digest_version=1, request_sha256=b"\x00" * 32
        )

        second = client.post("/api/v1/vault/contributions", json=body, headers=headers)

        assert second.status_code == 200, second.text
        assert second.json()["idempotent_replay"] is True
        assert second.json()["note_id"] == first.json()["note_id"]
        # Restating a digest is two columns; it must not have bought an
        # embedding call or a second document.
        assert provider.calls == calls_after_first

        stored = _stored_digests("test-principal-")
        assert stored == [
            (
                REQUEST_DIGEST_VERSION,
                _canonical_request_digest(
                    VaultContributionRequest.model_validate(body)
                ),
            )
        ]
    finally:
        _cleanup()


def test_a_restated_digest_makes_the_next_conflict_detectable_again(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The point of restating: the row stops being permanently uncomparable.

    A different body under the same key was undetectable while the row carried a
    retired rule. After one replay it is a 409 again.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        _force_stored_digest(
            "test-principal-", digest_version=1, request_sha256=b"\x00" * 32
        )

        replay = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert replay.status_code == 200, replay.text

        conflicting = {**body, "body": "Entirely different content."}
        third = client.post(
            "/api/v1/vault/contributions", json=conflicting, headers=headers
        )

        assert third.status_code == 409
    finally:
        _cleanup()


def test_a_mismatched_digest_under_the_current_rule_is_still_a_conflict(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The grandfather clause is scoped to the retired rule, not to mismatches.

    Same setup as the test above with only the stored version changed, so what
    is being asserted is that the version -- not the mismatch -- is what decides.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        _force_stored_digest(
            "test-principal-",
            digest_version=REQUEST_DIGEST_VERSION,
            request_sha256=b"\x00" * 32,
        )

        second = client.post("/api/v1/vault/contributions", json=body, headers=headers)

        assert second.status_code == 409
    finally:
        _cleanup()


def test_identical_content_is_flagged_rather_than_duplicated(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The shipped policy flags only an exact match — this is that case.

    Same title and body under a *different* idempotency key is not a retry; it
    is a genuine second contribution of identical content, and the dedup gate
    is what catches it.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        duplicate = {**body, "idempotency_key": f"key-{uuid4().hex}"}
        second = client.post(
            "/api/v1/vault/contributions", json=duplicate, headers=headers
        )

        assert second.status_code == 200, second.text
        payload = second.json()

        assert payload["status"] == "flagged"
        assert payload["similars"], "a flag must report what it matched"
        assert payload["similars"][0]["note_id"] == first.json()["note_id"]

        # Flagged is a successful write: the note exists for adjudication, and
        # ADR 0008 keeps the read surface from serving it meanwhile.
        withheld = client.get(
            f"/api/v1/vault/notes/{payload['note_id']}", headers=headers
        )
        assert withheld.status_code == 404
    finally:
        _cleanup()


def test_a_read_only_credential_cannot_contribute(
    client: TestClient, provider: StubEmbeddingProvider, configure_test_env: None
) -> None:
    credential_id, token = _issue(scopes=(VaultScope.READ,))
    try:
        response = client.post(
            "/api/v1/vault/contributions",
            json=_payload(),
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 403


def test_contribution_is_refused_when_no_embedding_provider_is_configured(
    client: TestClient, write_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dedup, no write.

    The read path degrades to lexical-only and reports it. The write path must
    not degrade to *no dedup*: that silently defeats the gate the vault exists
    to enforce, and a corpus quietly accreting duplicates is not recoverable
    the way a refused contribution is.
    """

    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: None)

    response = client.post(
        "/api/v1/vault/contributions",
        json=_payload(),
        headers={"Authorization": f"Bearer {write_token}"},
    )

    assert response.status_code == 503


def test_transport_validation_still_rejects_a_malformed_request(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    empty_title = client.post(
        "/api/v1/vault/contributions",
        json=_payload(title=""),
        headers=headers,
    )
    short_key = client.post(
        "/api/v1/vault/contributions",
        json=_payload(idempotency_key="short"),
        headers=headers,
    )
    duplicate_tags = client.post(
        "/api/v1/vault/contributions",
        json=_payload(tags=["same", "same"]),
        headers=headers,
    )

    assert empty_title.status_code == 422
    assert short_key.status_code == 422
    assert duplicate_tags.status_code == 422


def test_facets_and_relations_round_trip_through_the_write_path(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The v1 contract carries classification and note-to-note links.

    Before ADR 0017 none of these were reachable: the request model carried
    only title, body, tags, and source_url, so `related_ids` and `source_ids`
    existed in the schema with zero rows using them.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload(
        summary="A short precis.",
        aliases=["Package cache expansion"],
        facets={"project": ["highscoreserver"], "area": ["backend"]},
        related_ids=["some-other-note"],
        source_ids=["a-source-note"],
    )

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=body, headers=headers
        )
        assert response.status_code == 200, response.text

        detail = client.get(
            f"/api/v1/vault/notes/{response.json()['note_id']}", headers=headers
        ).json()

        assert detail["facets"] == {
            "project": ["highscoreserver"],
            "area": ["backend"],
        }
        assert detail["related_ids"] == ["some-other-note"]
        assert detail["source_ids"] == ["a-source-note"]
        assert detail["aliases"] == ["Package cache expansion"]
        assert detail["summary"] == "A short precis."
    finally:
        _cleanup()


def test_an_unknown_facet_name_is_refused_as_invalid(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """A typo like 'projects' files a note where nothing will look for it.

    Governance validation failure is 422 per ADR 0016 -- distinct from
    Pydantic's transport 422 only in origin. It is not one of the *settled*
    outcomes (`flagged`, `rejected`) that return 200, because nothing landed.
    """

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        response = client.post(
            "/api/v1/vault/contributions",
            json=_payload(facets={"projects": ["typo"]}),
            headers=headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["message"] == "contribution failed validation"
        # The error names the known facets, so the typo is self-correcting.
        assert any("projects" in error for error in detail["errors"])
        assert any("project" in error for error in detail["errors"])
    finally:
        _cleanup()


def test_a_scalar_facet_value_is_refused_at_the_transport_boundary(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Shape is Pydantic's job, so this is a 422 rather than a settled outcome.

    Refused rather than coerced: accepting both {"project": "hss"} and
    {"project": ["hss"]} would make a containment query written for one
    silently miss the other.
    """

    headers = {"Authorization": f"Bearer {write_token}"}

    response = client.post(
        "/api/v1/vault/contributions",
        json=_payload(facets={"project": "highscoreserver"}),
        headers=headers,
    )

    assert response.status_code == 422


def test_facets_are_normalized_before_storage(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Sorted, de-duplicated, and stripped.

    A blank value never gets this far over HTTP -- the request model refuses
    it outright, matching how `tags` is handled. `normalize_facets` drops
    blanks anyway, for callers that are not this route and as defence in
    depth.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload(facets={"project": ["  beta ", "alpha", "beta"]})

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=body, headers=headers
        )
        assert response.status_code == 200, response.text

        detail = client.get(
            f"/api/v1/vault/notes/{response.json()['note_id']}", headers=headers
        ).json()

        assert detail["facets"] == {"project": ["alpha", "beta"]}
    finally:
        _cleanup()
