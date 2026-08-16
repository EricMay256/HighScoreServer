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
from sqlalchemy import delete, func, select, update
from sqlalchemy import text as text_sql
from sqlalchemy.ext.asyncio import AsyncConnection

from app.vault.api_models import VaultContributionRequest
from app.vault.auth import VaultScope  # noqa: E402  (grouped with vault imports)
from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
)
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.embeddings import (
    EmbeddingInputKind,
    EmbeddingInputTooLong,
    EmbeddingVector,
)
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
    VaultReviewCaseRepository,
)
from app.vault.routes import _canonical_request_digest
from app.vault.service import (
    _CONTRIBUTION_LOCK_KEY,
    REQUEST_DIGEST_VERSION,
    DocumentUnderReview,
    RetireRequest,
    UpdateRequest,
    VaultDocumentRetireService,
    VaultDocumentUpdateService,
)
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_audit_events,
    vault_document_embeddings,
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
        self.reject_input_over_chars: int | None = None

    async def embed(
        self, texts, kind: EmbeddingInputKind
    ) -> tuple[EmbeddingVector, ...]:
        del kind
        self.calls += 1
        if self.reject_input_over_chars is not None and any(
            len(text) > self.reject_input_over_chars for text in texts
        ):
            raise EmbeddingInputTooLong("stub context limit exceeded")
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
    """A credential holding every write verb.

    All three are granted because this fixture serves tests of contribute,
    replace *and* retire, and those are separate scopes since ADR 0020. That
    they are separable is asserted deliberately in the scope tests below rather
    than incidentally here — a fixture that under-grants would fail those tests
    for the wrong reason.
    """

    credential_id, token = _issue(
        scopes=(
            VaultScope.READ,
            VaultScope.WRITE,
            VaultScope.UPDATE,
            VaultScope.DELETE,
        )
    )
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

    first_headers = {
        "Authorization": f"Bearer {write_token}",
        "X-Request-Id": "contribution-first-attempt",
    }
    replay_headers = {
        "Authorization": f"Bearer {write_token}",
        "X-Request-Id": "contribution-replay-attempt",
    }
    body = _payload()

    try:
        first = client.post(
            "/api/v1/vault/contributions", json=body, headers=first_headers
        )
        assert first.status_code == 200, first.text
        calls_after_first = provider.calls

        second = client.post(
            "/api/v1/vault/contributions", json=body, headers=replay_headers
        )

        assert second.status_code == 200
        assert second.json()["note_id"] == first.json()["note_id"]
        assert second.json()["idempotent_replay"] is True
        # A replay must not buy an embedding call.
        assert provider.calls == calls_after_first
        assert _count(vault_documents, id=first.json()["note_id"]) == 1
        assert _count(
            vault_write_requests, idempotency_key=body["idempotency_key"]
        ) == 1

        service, engine = vault_service()

        async def audit_attempts() -> list[tuple[str, str]]:
            async with service.transaction() as connection:
                result = await connection.execute(
                    select(
                        vault_audit_events.c.request_id,
                        vault_audit_events.c.outcome,
                    )
                    .where(
                        vault_audit_events.c.idempotency_key
                        == body["idempotency_key"]
                    )
                    .order_by(vault_audit_events.c.id)
                )
                return [(str(row[0]), str(row[1])) for row in result.all()]

        try:
            attempts = asyncio.run(audit_attempts())
        finally:
            asyncio.run(engine.dispose())

        assert attempts == [
            ("contribution-first-attempt", "inserted"),
            ("contribution-replay-attempt", "replayed"),
        ]
    finally:
        _cleanup()


def test_reusing_a_key_for_a_different_body_is_a_conflict(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    first_headers = {
        "Authorization": f"Bearer {write_token}",
        "X-Request-Id": "idempotency-original-attempt",
    }
    conflict_headers = {
        "Authorization": f"Bearer {write_token}",
        "X-Request-Id": "idempotency-conflicting-attempt",
    }
    body = _payload()

    try:
        first = client.post(
            "/api/v1/vault/contributions", json=body, headers=first_headers
        )
        assert first.status_code == 200, first.text

        conflicting = {**body, "body": "Entirely different content."}
        response = client.post(
            "/api/v1/vault/contributions", json=conflicting, headers=conflict_headers
        )

        assert response.status_code == 409

        service, engine = vault_service()

        async def audit_attempts() -> list[tuple[str, str]]:
            async with service.transaction() as connection:
                result = await connection.execute(
                    select(
                        vault_audit_events.c.request_id,
                        vault_audit_events.c.outcome,
                    )
                    .where(
                        vault_audit_events.c.idempotency_key
                        == body["idempotency_key"]
                    )
                    .order_by(vault_audit_events.c.id)
                )
                return [(str(row[0]), str(row[1])) for row in result.all()]

        try:
            attempts = asyncio.run(audit_attempts())
        finally:
            asyncio.run(engine.dispose())

        assert attempts == [
            ("idempotency-original-attempt", "inserted"),
            ("idempotency-conflicting-attempt", "conflict"),
        ]
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
    canonical = model.model_dump(mode="json", exclude_unset=True)

    assert set(canonical) == set(supplied)
    for unsent in ("summary", "aliases", "facets", "related_ids", "source_ids"):
        assert unsent not in canonical

    expected = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert _canonical_request_digest(model) == sha256(expected.encode("utf-8")).digest()


def test_the_digest_recursively_sorts_nested_object_keys() -> None:
    key = f"key-{uuid4().hex}"
    first = VaultContributionRequest.model_validate(
        {
            "title": "Nested order",
            "body": "Same semantic request.",
            "facets": {"project": ["hss"], "area": ["backend"]},
            "idempotency_key": key,
        }
    )
    reversed_order = VaultContributionRequest.model_validate(
        {
            "title": "Nested order",
            "body": "Same semantic request.",
            "facets": {"area": ["backend"], "project": ["hss"]},
            "idempotency_key": key,
        }
    )

    assert _canonical_request_digest(first) == _canonical_request_digest(reversed_order)


# A payload frozen against the digest it must produce. Every declared field is
# supplied on purpose: `exclude_unset` emits only supplied fields, in *declaration*
# order, so covering all of them means any reordering of any two is caught.
_GOLDEN_PAYLOAD: dict[str, object] = {
    "title": "Golden payload",
    "body": "A fixed body, pinned so the digest rule cannot move unnoticed.",
    "summary": "A fixed summary.",
    "tags": ["golden", "digest"],
    "aliases": ["golden-note"],
    "facets": {"project": ["hss"]},
    "related_ids": ["rel-1"],
    "source_ids": ["src-1"],
    "source_url": "https://example.test/golden",
    "idempotency_key": "golden-key-0001",
}
_GOLDEN_DIGEST_VERSION = 3
_GOLDEN_DIGEST_HEX = "2b7e68311aae9c1fa1049af871e9b80b58da127ce07201e548886aec23ccdc88"


def test_the_digest_rule_is_pinned_to_a_golden_value() -> None:
    """A change to the digest rule must be a decision, not a side effect.

    The test above asserts the digest ignores unsupplied fields, but it computes
    its expectation the same way the function does -- so if the serialization
    *order* changes, both sides move together and it still passes. Pydantic emits
    in field-declaration order, so reordering or renaming a field in
    `VaultContributionRequest` silently changes the digest of every request, and
    every stored key then compares against a rule it was not written under. That
    is migration 0006's failure exactly: 409s no caller can clear.

    Note what this does *not* fire on. Adding an optional field anywhere in the
    model leaves this digest untouched, because an unsupplied field is not
    emitted regardless of its position -- which is the property `exclude_unset`
    exists to provide. So this is not a change-detector that cries at every edit;
    it fires on reorders and renames, which is precisely when a caller's stored
    digest stops meaning what it meant.

    **Both halves are pinned deliberately.** If this fails, the fix is not to
    paste in the new hash: it is to decide whether the rule changed, bump
    REQUEST_DIGEST_VERSION, and write the migration that lets old rows replay --
    then update both constants together. Asserting the version here is what makes
    that step unskippable.
    """

    model = VaultContributionRequest.model_validate(_GOLDEN_PAYLOAD)

    assert REQUEST_DIGEST_VERSION == _GOLDEN_DIGEST_VERSION, (
        "REQUEST_DIGEST_VERSION moved without the golden digest being restated. "
        "Update _GOLDEN_DIGEST_HEX in the same change, and confirm a migration "
        "exists for rows written under the previous rule."
    )
    assert _canonical_request_digest(model).hex() == _GOLDEN_DIGEST_HEX, (
        "The canonical request digest changed for a fixed payload. Every stored "
        "digest at this version was produced by the old rule, so bump "
        "REQUEST_DIGEST_VERSION and add the migration before restating this value."
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aliases", ["   "]),
        ("aliases", ["same", "same"]),
        ("related_ids", ["   "]),
        ("related_ids", ["same", "same"]),
        ("source_ids", ["   "]),
        ("source_ids", ["same", "same"]),
    ],
)
def test_update_shares_create_time_collection_validation(
    client: TestClient,
    write_token: str,
    provider: StubEmbeddingProvider,
    field: str,
    value: list[str],
) -> None:
    response = client.put(
        "/api/v1/vault/notes/not-reached",
        json=_update_payload(**{field: value}),
        headers={"Authorization": f"Bearer {write_token}"},
    )

    assert response.status_code == 422


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


@pytest.mark.parametrize("method", ["post", "put"])
def test_facet_names_that_collide_after_normalization_are_refused(
    client: TestClient,
    write_token: str,
    provider: StubEmbeddingProvider,
    method: str,
) -> None:
    payload = _payload(facets={" project": ["first"], "project": ["second"]})
    path = "/api/v1/vault/contributions"
    if method == "put":
        payload = _update_payload(
            facets={" project": ["first"], "project": ["second"]}
        )
        path = "/api/v1/vault/notes/not-reached"

    response = client.request(
        method,
        path,
        json=payload,
        headers={"Authorization": f"Bearer {write_token}"},
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


def _update_payload(**overrides) -> dict:
    base = {
        "title": f"Replacement {uuid4().hex[:8]}",
        "body": "A replacement body, distinctive enough not to collide.",
        "tags": ["testing"],
    }
    return {**base, **overrides}


def _contribute(client: TestClient, token: str, **overrides) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/v1/vault/contributions", json=_payload(**overrides), headers=headers
    )
    assert response.status_code == 200, response.text
    note_id = response.json()["note_id"]
    assert note_id is not None
    return note_id


def test_embedding_context_rejection_is_422_for_create_and_update(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        provider.reject_input_over_chars = 100

        create = client.post(
            "/api/v1/vault/contributions",
            json=_payload(body="x" * 200),
            headers=headers,
        )
        replacement = client.put(
            f"/api/v1/vault/notes/{note_id}",
            json=_update_payload(body="x" * 200),
            headers=headers,
        )

        assert create.status_code == 422
        assert replacement.status_code == 422
        assert "embedding model input limit" in create.json()["detail"]
        assert "embedding model input limit" in replacement.json()["detail"]
    finally:
        _cleanup()


def test_an_update_replaces_content_and_re_embeds(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        calls_after_contribute = provider.calls

        replacement = _update_payload(tags=["testing", "replaced"])
        response = client.put(
            f"/api/v1/vault/notes/{note_id}", json=replacement, headers=headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["note_id"] == note_id
        assert response.json()["re_embedded"] is True
        # The embedding text moved, so exactly one further call was bought.
        assert provider.calls == calls_after_contribute + 1

        fetched = client.get(f"/api/v1/vault/notes/{note_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["title"] == replacement["title"]
        assert fetched.json()["body"] == replacement["body"]
        assert sorted(fetched.json()["tags"]) == ["replaced", "testing"]
    finally:
        _cleanup()


def test_an_update_touching_only_unembedded_fields_does_not_re_embed(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """ADR 0013's whole point: facets are not in the embedding text.

    This is the case a facet backfill runs, so it must not pay for an embedding
    call per document.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    original = _payload()

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=original, headers=headers
        )
        assert response.status_code == 200, response.text
        note_id = response.json()["note_id"]
        calls_after_contribute = provider.calls

        # Same title/body/tags; only facets differ.
        replacement = {
            "title": original["title"],
            "body": original["body"],
            "tags": original["tags"],
            "facets": {"project": ["highscoreserver"]},
        }
        updated = client.put(
            f"/api/v1/vault/notes/{note_id}", json=replacement, headers=headers
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["re_embedded"] is False
        assert provider.calls == calls_after_contribute

        fetched = client.get(f"/api/v1/vault/notes/{note_id}", headers=headers)
        assert fetched.json()["facets"] == {"project": ["highscoreserver"]}
    finally:
        _cleanup()


def test_concurrent_updates_keep_final_text_and_embedding_together(
    client: TestClient,
    write_token: str,
    provider: StubEmbeddingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale no-reembed decision must not preserve another update's vector."""

    original = _payload()
    try:
        created = client.post(
            "/api/v1/vault/contributions",
            json=original,
            headers={"Authorization": f"Bearer {write_token}"},
        )
        assert created.status_code == 200, created.text
        note_id = created.json()["note_id"]

        async def exercise() -> None:
            transactions, engine = vault_service()
            updater = VaultDocumentUpdateService(transactions, provider)
            original_get = VaultDocumentEmbeddingRepository.get
            stale_snapshot_loaded = asyncio.Event()
            release_stale_update = asyncio.Event()

            async def delayed_get(
                repository: VaultDocumentEmbeddingRepository,
                connection: AsyncConnection,
                document_id: str,
                profile_id: str,
            ) -> DocumentEmbedding | None:
                stored = await original_get(
                    repository, connection, document_id, profile_id
                )
                task = asyncio.current_task()
                if task is not None and task.get_name() == "restore-update":
                    stale_snapshot_loaded.set()
                    await release_stale_update.wait()
                return stored

            monkeypatch.setattr(VaultDocumentEmbeddingRepository, "get", delayed_get)

            restore = UpdateRequest(
                document_id=note_id,
                title=original["title"],
                body=original["body"],
                tags=tuple(original["tags"]),
                facets={"project": ["highscoreserver"]},
                principal_id="test-principal-race",
                request_id="restore-update",
            )
            changed = UpdateRequest(
                document_id=note_id,
                title="Concurrent replacement",
                body="The competing update changes the embedding text.",
                tags=("testing",),
                principal_id="test-principal-race",
                request_id="changed-update",
            )

            restore_task = asyncio.create_task(
                updater.update(restore), name="restore-update"
            )
            try:
                await stale_snapshot_loaded.wait()
                changed_outcome = await updater.update(changed)
                assert changed_outcome.re_embedded is True
            finally:
                release_stale_update.set()

            restore_outcome = await restore_task
            assert restore_outcome.re_embedded is False

            documents = VaultDocumentRepository()
            embeddings = VaultDocumentEmbeddingRepository()
            async with transactions.transaction() as connection:
                final_document = await documents.get_by_id(connection, note_id)
                final_embedding = await embeddings.get(
                    connection, note_id, provider.profile_id
                )

            assert final_document is not None
            assert final_embedding is not None
            assert final_document.title == original["title"]
            final_text = assemble_embedding_text(final_document)
            assert final_embedding.text_sha256 == embedding_text_digest(final_text)
            assert final_embedding.vector == provider._vector(final_text)
            await engine.dispose()

        asyncio.run(exercise())
    finally:
        _cleanup()


def test_an_update_is_not_a_duplicate_of_itself(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Dedup excludes the document being updated.

    Without the exclusion the candidate scores 1.0 against its own stored
    vector and every no-op edit is refused. Resending identical content is the
    sharpest form of the case.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    original = _payload()

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=original, headers=headers
        )
        assert response.status_code == 200, response.text
        note_id = response.json()["note_id"]

        identical = {
            "title": original["title"],
            "body": original["body"],
            "tags": original["tags"],
        }
        updated = client.put(
            f"/api/v1/vault/notes/{note_id}", json=identical, headers=headers
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["re_embedded"] is False
    finally:
        _cleanup()


def test_an_update_colliding_with_another_document_is_refused(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The gate still applies to *other* documents.

    Editing one note into an exact copy of another is the way around dedup that
    an unguarded update surface would open. It refuses rather than flagging, so
    the document being edited is left exactly as it was.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    first = _payload()

    try:
        created = client.post(
            "/api/v1/vault/contributions", json=first, headers=headers
        )
        assert created.status_code == 200, created.text

        victim_id = _contribute(client, write_token)
        before = client.get(f"/api/v1/vault/notes/{victim_id}", headers=headers).json()

        collision = {
            "title": first["title"],
            "body": first["body"],
            "tags": first["tags"],
        }
        refused = client.put(
            f"/api/v1/vault/notes/{victim_id}", json=collision, headers=headers
        )

        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["similars"], "the collision should name what it hit"

        # Refused means nothing was written.
        after = client.get(f"/api/v1/vault/notes/{victim_id}", headers=headers).json()
        assert after["title"] == before["title"]
        assert after["body"] == before["body"]
        assert after["updated_at"] == before["updated_at"]
    finally:
        _cleanup()


def test_updating_a_missing_document_is_404(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        response = client.put(
            f"/api/v1/vault/notes/{uuid4().hex}",
            json=_update_payload(),
            headers=headers,
        )
        assert response.status_code == 404
    finally:
        _cleanup()


def test_an_update_requires_the_write_scope(
    client: TestClient, provider: StubEmbeddingProvider, configure_test_env: None
) -> None:
    credential_id, read_only = _issue(scopes=(VaultScope.READ,))
    try:
        response = client.put(
            f"/api/v1/vault/notes/{uuid4().hex}",
            json=_update_payload(),
            headers={"Authorization": f"Bearer {read_only}"},
        )
        assert response.status_code == 403
    finally:
        _drop(credential_id)
        _cleanup()


def test_an_update_does_not_change_the_contributor(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Who wrote a note and who edited it are different facts.

    ``contributed_by`` is the author; the editor lands in the audit trail. An
    update that overwrote it would erase authorship on every correction.
    """

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)

        service, engine = vault_service()

        async def read_contributor() -> str:
            async with service.transaction() as connection:
                result = await connection.execute(
                    select(vault_documents.c.contributed_by).where(
                        vault_documents.c.id == note_id
                    )
                )
                return str(result.scalar_one())

        try:
            before = asyncio.run(read_contributor())
        finally:
            asyncio.run(engine.dispose())

        updated = client.put(
            f"/api/v1/vault/notes/{note_id}",
            json=_update_payload(),
            headers=headers,
        )
        assert updated.status_code == 200, updated.text

        service, engine = vault_service()
        try:
            after = asyncio.run(read_contributor())
        finally:
            asyncio.run(engine.dispose())

        assert after == before
    finally:
        _cleanup()


def test_an_update_records_an_audit_event(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        updated = client.put(
            f"/api/v1/vault/notes/{note_id}",
            json=_update_payload(),
            headers=headers,
        )
        assert updated.status_code == 200, updated.text

        service, engine = vault_service()

        async def operations() -> list[str]:
            async with service.transaction() as connection:
                result = await connection.execute(
                    select(vault_audit_events.c.operation).where(
                        vault_audit_events.c.target_id == note_id
                    )
                )
                return [str(row[0]) for row in result.all()]

        try:
            recorded = asyncio.run(operations())
        finally:
            asyncio.run(engine.dispose())

        assert "vault.update" in recorded
    finally:
        _cleanup()


def _count(table, **where):
    service, engine = vault_service()

    async def run() -> int:
        async with service.transaction() as connection:
            statement = select(func.count()).select_from(table)
            for column, value in where.items():
                statement = statement.where(table.c[column] == value)
            result = await connection.execute(statement)
            return int(result.scalar_one())

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())


def test_retiring_a_document_removes_it_and_its_embedding(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        assert _count(vault_document_embeddings, document_id=note_id) == 1

        response = client.delete(f"/api/v1/vault/notes/{note_id}", headers=headers)

        assert response.status_code == 204, response.text
        assert response.content == b""
        assert _count(vault_documents, id=note_id) == 0
        # The embedding FK cascades: a vector for a document that no longer
        # exists is not a record of anything.
        assert _count(vault_document_embeddings, document_id=note_id) == 0
        assert client.get(
            f"/api/v1/vault/notes/{note_id}", headers=headers
        ).status_code == 404
    finally:
        _cleanup()


def test_retiring_keeps_the_write_request_and_clears_its_pointer(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The ledger row outlives the document, deliberately.

    It is what makes a replayed idempotency key a no-op. Deleting it would let a
    retired document be recreated by a retry, which is the opposite of retiring.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        created = client.post(
            "/api/v1/vault/contributions", json=body, headers=headers
        )
        assert created.status_code == 200, created.text
        note_id = created.json()["note_id"]
        assert _count(vault_write_requests, document_id=note_id) == 1

        client.delete(f"/api/v1/vault/notes/{note_id}", headers=headers)

        assert _count(vault_write_requests, document_id=note_id) == 0
        assert _count(
            vault_write_requests, idempotency_key=body["idempotency_key"]
        ) == 1
    finally:
        _cleanup()


def test_retiring_records_an_audit_event_that_outlives_the_document(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        client.delete(f"/api/v1/vault/notes/{note_id}", headers=headers)

        service, engine = vault_service()

        async def operations() -> list[str]:
            async with service.transaction() as connection:
                result = await connection.execute(
                    select(vault_audit_events.c.operation).where(
                        vault_audit_events.c.target_id == note_id
                    )
                )
                return [str(row[0]) for row in result.all()]

        try:
            recorded = asyncio.run(operations())
        finally:
            asyncio.run(engine.dispose())

        assert "vault.retire" in recorded
    finally:
        _cleanup()


def test_retiring_a_missing_document_is_404(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    try:
        response = client.delete(
            f"/api/v1/vault/notes/{uuid4().hex}",
            headers={"Authorization": f"Bearer {write_token}"},
        )
        assert response.status_code == 404
    finally:
        _cleanup()


def test_retiring_requires_the_write_scope(
    client: TestClient, provider: StubEmbeddingProvider, configure_test_env: None
) -> None:
    credential_id, read_only = _issue(scopes=(VaultScope.READ,))
    try:
        response = client.delete(
            f"/api/v1/vault/notes/{uuid4().hex}",
            headers={"Authorization": f"Bearer {read_only}"},
        )
        assert response.status_code == 403
    finally:
        _drop(credential_id)
        _cleanup()


def test_a_document_under_review_cannot_be_retired(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """A review case is a judgement, and its FK does not cascade.

    Deleting under it would either fail on the constraint or leave a decision
    pointing at nothing, so the retire path refuses and says why.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        first = client.post("/api/v1/vault/contributions", json=body, headers=headers)
        assert first.status_code == 200, first.text

        # Same content under a new key: the shipped policy flags an exact match,
        # which is what opens a review case.
        duplicate = {**body, "idempotency_key": f"key-{uuid4().hex}"}
        flagged = client.post(
            "/api/v1/vault/contributions", json=duplicate, headers=headers
        )
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["status"] == "flagged"
        candidate = flagged.json()["note_id"]

        response = client.delete(
            f"/api/v1/vault/notes/{candidate}", headers=headers
        )

        # Flagged documents are outside READABLE_STATUSES, so the retire path
        # cannot see it at all -- 404 rather than 409. Either way it is refused
        # and the row survives; correcting a flagged document belongs to the
        # review surface, which is unbuilt.
        assert response.status_code in (404, 409)
        assert _count(vault_documents, id=candidate) == 1
    finally:
        _cleanup()


def test_review_evidence_document_cannot_be_retired(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}
    body = _payload()

    try:
        original = client.post(
            "/api/v1/vault/contributions", json=body, headers=headers
        )
        duplicate = client.post(
            "/api/v1/vault/contributions",
            json={**body, "idempotency_key": f"key-{uuid4().hex}"},
            headers=headers,
        )
        assert original.status_code == 200, original.text
        assert duplicate.json()["status"] == "flagged"

        original_id = original.json()["note_id"]
        response = client.delete(
            f"/api/v1/vault/notes/{original_id}", headers=headers
        )

        assert response.status_code == 409, response.text
        assert _count(vault_documents, id=original_id) == 1
    finally:
        _cleanup()


def test_resolved_candidate_document_still_cannot_be_retired(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    note_id = _contribute(client, write_token)

    try:

        async def add_resolved_case() -> None:
            transactions, engine = vault_service()
            try:
                async with transactions.transaction() as connection:
                    await VaultReviewCaseRepository().insert_pending(
                        connection,
                        candidate_document_id=note_id,
                        reason="Resolved candidate remains durable review history",
                        similar_documents=[],
                    )
                    await connection.execute(
                        update(vault_review_cases)
                        .where(
                            vault_review_cases.c.candidate_document_id == note_id
                        )
                        .values(
                            state=ReviewState.ACCEPTED.value,
                            decided_at=func.now(),
                            decided_by="test:reviewer",
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(add_resolved_case())
        response = client.delete(
            f"/api/v1/vault/notes/{note_id}",
            headers={"Authorization": f"Bearer {write_token}"},
        )

        assert response.status_code == 409, response.text
        assert _count(vault_documents, id=note_id) == 1
    finally:
        _cleanup()


def test_resolved_review_evidence_no_longer_blocks_retirement(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    evidence_id = _contribute(client, write_token)
    candidate_id = f"resolved-evidence-{uuid4().hex}"

    try:

        async def add_resolved_case() -> None:
            transactions, engine = vault_service()
            try:
                async with transactions.transaction() as connection:
                    await VaultDocumentRepository().insert(
                        connection,
                        NewVaultDocument(
                            id=candidate_id,
                            kind=DocumentKind.NOTE,
                            vault_path=f"Agent/notes/{candidate_id}.md",
                            status=DocumentStatus.FLAGGED,
                            title="Resolved review candidate",
                            body="A settled case may release its supporting evidence.",
                            contributed_by="agent:test-principal-resolved-evidence",
                            provenance={"fixture": True},
                        ),
                    )
                    await VaultReviewCaseRepository().insert_pending(
                        connection,
                        candidate_document_id=candidate_id,
                        reason="Resolved evidence fixture",
                        similar_documents=[
                            {
                                "note_id": evidence_id,
                                "title": "Evidence",
                                "score": 1.0,
                            }
                        ],
                    )
                    await connection.execute(
                        update(vault_review_cases)
                        .where(
                            vault_review_cases.c.candidate_document_id == candidate_id
                        )
                        .values(
                            state=ReviewState.REJECTED.value,
                            decided_at=func.now(),
                            decided_by="test:reviewer",
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(add_resolved_case())
        response = client.delete(
            f"/api/v1/vault/notes/{evidence_id}",
            headers={"Authorization": f"Bearer {write_token}"},
        )

        assert response.status_code == 204, response.text
        assert _count(vault_documents, id=evidence_id) == 0
    finally:
        _cleanup()


def test_retirement_serializes_with_creation_of_review_evidence(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    original_id = _contribute(client, write_token)

    try:
        async def exercise() -> None:
            transactions, engine = vault_service()
            retire = VaultDocumentRetireService(transactions)
            candidate_id = f"race-candidate-{uuid4().hex}"

            try:
                async with transactions.transaction() as connection:
                    await connection.execute(
                        text_sql("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": _CONTRIBUTION_LOCK_KEY},
                    )
                    retirement = asyncio.create_task(
                        retire.retire(
                            RetireRequest(
                                document_id=original_id,
                                principal_id="test-principal-race",
                                request_id="concurrent-retire",
                            )
                        )
                    )
                    await asyncio.sleep(0.05)
                    assert retirement.done() is False

                    await VaultDocumentRepository().insert(
                        connection,
                        NewVaultDocument(
                            id=candidate_id,
                            kind=DocumentKind.NOTE,
                            vault_path=f"Agent/notes/{candidate_id}.md",
                            status=DocumentStatus.FLAGGED,
                            title="Concurrent review candidate",
                            body="Created while retirement waits for the corpus lock.",
                            contributed_by="agent:test-principal-race",
                            provenance={"fixture": True},
                        ),
                    )
                    await VaultReviewCaseRepository().insert_pending(
                        connection,
                        candidate_document_id=candidate_id,
                        reason="Concurrent evidence race",
                        similar_documents=[
                            {"note_id": original_id, "title": "Original", "score": 1.0}
                        ],
                    )

                with pytest.raises(DocumentUnderReview):
                    await retirement
            finally:
                await engine.dispose()

        asyncio.run(exercise())
        assert _count(vault_documents, id=original_id) == 1
    finally:
        _cleanup()


# --- Write scopes are separable (ADR 0020) ---------------------------------


def _seeded_note(client: TestClient, token: str) -> str:
    """Contribute one note and return its id, for the scope tests to target."""

    response = client.post(
        "/api/v1/vault/contributions",
        json={
            "title": f"Scope fixture {uuid4().hex[:8]}",
            "body": "A note that exists so a scope check has something to aim at.",
            "idempotency_key": f"scope-{uuid4().hex}",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["note_id"]


def test_contribute_scope_does_not_grant_replacement(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """vault:write is contribute only.

    Before ADR 0020 this returned 200: one scope gated all three write routes,
    so any credential that could add a note could also overwrite one.
    """

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        note_id = _seeded_note(client, token)
        response = client.put(
            f"/api/v1/vault/notes/{note_id}",
            json={"title": "Replaced", "body": "Replaced body."},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)
        _cleanup()

    assert response.status_code == 403


def test_contribute_scope_does_not_grant_deletion(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """The one that matters: adding a note must not imply destroying one."""

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        note_id = _seeded_note(client, token)
        response = client.delete(
            f"/api/v1/vault/notes/{note_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)
        _cleanup()

    assert response.status_code == 403


def test_update_scope_does_not_grant_deletion(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Replacement and deletion are separate verbs, not a ladder.

    An importer-shaped credential -- contribute and replace, never delete -- is
    the case this split exists to make expressible.
    """

    note_id = _seeded_note(client, write_token)
    credential_id, token = _issue(
        scopes=(VaultScope.READ, VaultScope.WRITE, VaultScope.UPDATE)
    )
    try:
        replaced = client.put(
            f"/api/v1/vault/notes/{note_id}",
            json={"title": "Replaced", "body": "A replacement body."},
            headers={"Authorization": f"Bearer {token}"},
        )
        refused = client.delete(
            f"/api/v1/vault/notes/{note_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)
        _cleanup()

    assert replaced.status_code == 200, replaced.text
    assert refused.status_code == 403


def test_delete_scope_alone_does_not_grant_contribution(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """Scopes are verbs, not privilege levels: delete does not imply write."""

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.DELETE))
    try:
        response = client.post(
            "/api/v1/vault/contributions",
            json={
                "title": "Should not land",
                "body": "A contribution from a delete-only credential.",
                "idempotency_key": f"scope-{uuid4().hex}",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)
        _cleanup()

    assert response.status_code == 403
