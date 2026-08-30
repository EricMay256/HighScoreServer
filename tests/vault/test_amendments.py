"""Revision-bound amendment proposals over the HTTP surface."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.vault.auth import VaultScope
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
)
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.embeddings import EmbeddingInputKind
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
)
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_amendment_proposals,
    vault_audit_events,
    vault_document_embeddings,
    vault_documents,
)
from tests.vault.test_contributions import StubEmbeddingProvider
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PREFIX = "test-amendment-"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> StubEmbeddingProvider:
    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: stub)
    return stub


@pytest.fixture
def tokens(configure_test_env: None) -> tuple[str, str, str]:
    issued = [
        _issue(scopes=(VaultScope.READ, VaultScope.PROPOSE)),
        _issue(scopes=(VaultScope.READ, VaultScope.REVIEW)),
        _issue(scopes=(VaultScope.READ,)),
    ]
    try:
        yield tuple(token for _, token in issued)
    finally:
        for credential_id, _ in issued:
            _drop(credential_id)


def _seed_note() -> str:
    note_id = f"{PREFIX}{uuid4().hex}"
    transactions, engine = vault_service()

    async def seed() -> None:
        try:
            async with transactions.transaction() as connection:
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=note_id,
                        kind=DocumentKind.NOTE,
                        doc_type="Agent Note",
                        vault_path=f"Agent/notes/{note_id}.md",
                        status=DocumentStatus.ACTIVE,
                        doc_status="Active",
                        title="Shell transport constraints",
                        body="The original note body.",
                        tags=("shell",),
                        contributed_by=f"agent:{PREFIX}seed",
                        provenance={"fixture": True},
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    return note_id


def _ensure_embedded(note_id: str, provider) -> None:
    """Store the vector a contributed note would already have.

    The amendment fixtures insert documents directly, so they carry no row in
    `vault_document_embeddings`. Any update to such a note embeds -- correctly,
    since there is nothing to reuse -- which would otherwise make "this change
    spent no embedding call" untestable against these fixtures.
    """

    transactions, engine = vault_service()

    async def store() -> None:
        try:
            async with transactions.transaction() as connection:
                document = await VaultDocumentRepository().get_by_id(
                    connection, note_id, statuses=(DocumentStatus.ACTIVE,)
                )
                text = assemble_embedding_text(document)
                vector = await provider.embed(
                    [text], EmbeddingInputKind.DOCUMENT
                )
                await VaultDocumentEmbeddingRepository().upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=note_id,
                        profile_id=provider.profile_id,
                        vector=vector[0],
                        text_sha256=embedding_text_digest(text),
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(store())
    provider.calls = 0


def _cleanup() -> None:
    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(
                    delete(vault_amendment_proposals).where(
                        vault_amendment_proposals.c.target_document_id.like(
                            f"{PREFIX}%"
                        )
                    )
                )
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.like(f"{PREFIX}%")
                    )
                )
                await connection.execute(
                    delete(vault_audit_events).where(
                        vault_audit_events.c.operation.like("vault.amendment.%")
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())


def _proposal(note_id: str, *, body: str = "The amended note body.") -> dict:
    return {
        "target_note_id": note_id,
        "base_revision": 1,
        "change": {
            "kind": "replacement",
            "replacement": {
                "title": "Shell transport constraints",
                "body": body,
                "tags": ["shell", "powershell"],
            },
        },
        "rationale": "Add the platform-specific failure mode to the existing note.",
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_proposal_submission_has_its_own_scope(
    client: TestClient,
    tokens: tuple[str, str, str],
) -> None:
    proposer, _, reader = tokens
    note_id = _seed_note()
    try:
        denied = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(reader),
            json=_proposal(note_id),
        )
        accepted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=_proposal(note_id),
        )

        assert denied.status_code == 403
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["proposal"]["state"] == "pending"
    finally:
        _cleanup()


def test_review_accepts_exact_replacement_and_increments_revision(
    client: TestClient,
    tokens: tuple[str, str, str],
    provider: StubEmbeddingProvider,
) -> None:
    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=_proposal(note_id),
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        queue = client.get(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(reviewer),
        )
        assert queue.status_code == 200, queue.text
        summary = next(
            item for item in queue.json()["pending"] if item["proposal_id"] == proposal_id
        )
        assert "replacement" not in summary

        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["target"]["content_revision"] == 1
        assert detail.json()["change"]["kind"] == "replacement"
        assert (
            detail.json()["change"]["replacement"]["body"]
            == "The amended note body."
        )
        assert detail.json()["preview"]["resulting_body"] == "The amended note body."
        assert detail.json()["preview"]["removed_line_count"] == 1
        assert detail.json()["preview"]["requires_removal_acknowledgement"] is True

        refused = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert refused.status_code == 409, refused.text
        assert provider.calls == 0

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={
                "decision": "accepted",
                "decision_note": "Useful consolidation.",
                "acknowledge_removals": True,
            },
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "accepted"
        assert decision.json()["proposal"]["applied_revision"] == 2
        assert decision.json()["proposal"]["removals_acknowledged"] is True
        assert decision.json()["target"]["body"] == "The amended note body."
        assert decision.json()["target"]["content_revision"] == 2
        assert provider.calls == 1
    finally:
        _cleanup()


def test_review_accepts_compact_body_diff_addition(
    client: TestClient,
    tokens: tuple[str, str, str],
    provider: StubEmbeddingProvider,
) -> None:
    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    proposal = {
        "target_note_id": note_id,
        "base_revision": 1,
        "change": {
            "kind": "body_diff",
            "body_diff": "@@ -1 +1,3 @@\n The original note body.\n+\n+PowerShell does not support POSIX heredocs.",
        },
        "rationale": "Add the platform-specific failure mode.",
    }
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=proposal,
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        assert submitted.json()["proposal"]["change_kind"] == "body_diff"

        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["change"] == proposal["change"]
        assert "title" not in detail.json()["change"]
        assert detail.json()["preview"]["removed_line_count"] == 0
        assert detail.json()["preview"]["requires_removal_acknowledgement"] is False

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "accepted"
        assert decision.json()["proposal"]["removals_acknowledged"] is False
        assert decision.json()["target"]["body"] == (
            "The original note body.\n\nPowerShell does not support POSIX heredocs."
        )
        assert decision.json()["target"]["content_revision"] == 2
        assert provider.calls == 1
    finally:
        _cleanup()


def test_body_diff_removal_requires_explicit_reviewer_acknowledgement(
    client: TestClient,
    tokens: tuple[str, str, str],
    provider: StubEmbeddingProvider,
) -> None:
    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    proposal = {
        "target_note_id": note_id,
        "base_revision": 1,
        "change": {
            "kind": "body_diff",
            "body_diff": (
                "@@ -1 +1 @@\n"
                "-The original note body.\n"
                "+The corrected note body."
            ),
        },
        "rationale": "Correct the established guidance.",
    }
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=proposal,
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["preview"]["removed_lines"] == [
            {"line_number": 1, "text": "The original note body."}
        ]
        assert "-The original note body." in detail.json()["preview"]["unified_diff"]

        refused = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert refused.status_code == 409, refused.text
        assert "acknowledge_removals=true" in refused.json()["detail"]
        assert provider.calls == 0

        accepted = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted", "acknowledge_removals": True},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["target"]["body"] == "The corrected note body."
        assert accepted.json()["proposal"]["removals_acknowledged"] is True
        assert provider.calls == 1
    finally:
        _cleanup()


def test_acceptance_settles_stale_instead_of_overwriting(
    client: TestClient,
    tokens: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposer, reviewer, _ = tokens
    # Detecting staleness is a revision check, not a write, so it must remain
    # possible in a deployment whose embedding provider is temporarily absent.
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: None)
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=_proposal(note_id, body="A now-stale replacement."),
        )
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        transactions, engine = vault_service()

        async def concurrent_edit() -> None:
            try:
                async with transactions.transaction() as connection:
                    await connection.execute(
                        update(vault_documents)
                        .where(vault_documents.c.id == note_id)
                        .values(
                            body="A newer edit that must survive.",
                            content_revision=vault_documents.c.content_revision + 1,
                            updated_at=func.now(),
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(concurrent_edit())

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "stale"
        assert decision.json()["target"]["body"] == "A newer edit that must survive."
        assert decision.json()["target"]["content_revision"] == 2
    finally:
        _cleanup()


def test_rejection_needs_no_embedding_provider(
    client: TestClient,
    tokens: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposer, reviewer, _ = tokens
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: None)
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=_proposal(note_id),
        )
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "rejected"},
        )

        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "rejected"
        assert decision.json()["target"]["content_revision"] == 1
    finally:
        _cleanup()


def test_review_accepts_a_metadata_proposal_without_touching_content(
    client: TestClient,
    tokens: tuple[str, str, str],
    provider: StubEmbeddingProvider,
) -> None:
    """Propose to accept, end to end, for the metadata kind (ADR 0036).

    The acceptance branch is the one piece of this kind whose correctness was
    otherwise only structural: `_update_request` materialises a stored metadata
    payload into an `UpdateRequest` built from the target, and nothing proved
    that the result carries the target's body rather than an empty one.

    The embedding-call count is the load-bearing assertion. Every field this
    kind accepts is excluded from `assemble_embedding_text`, so the update
    path's digest comparison must find the text unchanged and skip
    re-embedding. An unchanged count is therefore evidence of the guarantee,
    not a performance note — see the assertion message for what a failure
    means, because the obvious response to it is the wrong one.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    # `_seed_note` inserts the row without an embedding, and the update path
    # correctly embeds a note that has no vector. Give it one first, so the
    # assertion below measures the metadata change rather than the fixture.
    _ensure_embedded(note_id, provider)

    proposal = {
        "target_note_id": note_id,
        "base_revision": 1,
        "change": {
            "kind": "metadata",
            "related_ids": ["a" * 32, "b" * 32],
            "facets": {"area": ["architecture"]},
        },
        "rationale": "Connect this note to the two it depends on.",
    }
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=proposal,
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        assert submitted.json()["proposal"]["change_kind"] == "metadata"

        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )
        assert detail.status_code == 200, detail.text
        # The reviewer sees the change, not a document to diff by eye.
        assert detail.json()["change"]["kind"] == "metadata"
        assert detail.json()["change"]["related_ids"] == ["a" * 32, "b" * 32]
        assert "title" not in detail.json()["change"]
        assert "body" not in detail.json()["change"]

        calls_before = provider.calls
        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "accepted"

        target = decision.json()["target"]
        assert target["related_ids"] == ["a" * 32, "b" * 32]
        assert target["facets"] == {"area": ["architecture"]}
        assert target["content_revision"] == 2
        # Content survived the round trip through storage and materialisation.
        assert target["body"] == "The original note body."
        assert target["title"]

        assert provider.calls == calls_before, (
            "Accepting a metadata proposal spent an embedding call. Do not fix "
            "this by changing the expected count -- that retires the guarantee "
            "rather than restoring it. Exactly one of two things has broken: "
            "either the metadata payload now admits a field that joins "
            "`assemble_embedding_text` (ADR 0036 permits only related_ids, "
            "source_ids, facets and source_url, none of which are embedded), "
            "or the update path no longer skips re-embedding when the stored "
            "digest matches. Both mean a metadata edit can now change what a "
            "note means to search, which is the one thing this kind promises "
            "it cannot do."
        )
    finally:
        _cleanup()


def test_an_accepted_metadata_proposal_leaves_untouched_fields_alone(
    client: TestClient,
    tokens: tuple[str, str, str],
    provider: StubEmbeddingProvider,
) -> None:
    """Omitted means unchanged has to survive storage, not just the dataclass.

    The payload stores only the keys the proposer set, so acceptance has to
    reconstruct "leave this alone" from an absent key rather than from a null.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": ["c" * 32]},
                "rationale": "Add one edge and nothing else.",
            },
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        before = client.get(
            f"/api/v1/vault/notes/{note_id}", headers=_headers(reviewer)
        ).json()

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert decision.status_code == 200, decision.text

        target = decision.json()["target"]
        assert target["related_ids"] == ["c" * 32]
        # Everything the proposal did not name is exactly as it was.
        assert target["source_ids"] == before["source_ids"]
        assert target["facets"] == before["facets"]
        assert target["tags"] == before["tags"]
        assert target["summary"] == before["summary"]
        assert target["body"] == before["body"]
    finally:
        _cleanup()


def test_http_patch_applies_metadata_directly_for_an_update_credential(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """The REST counterpart of `vault_update_note_metadata`.

    Without it a REST caller holding `vault:update` still has to resend the
    body to add one edge — which is the failure this whole path removes, and
    the runbook designates REST as the supported route for agents without MCP.
    """

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.UPDATE))
    note_id = _seed_note()
    _ensure_embedded(note_id, provider)
    try:
        before = client.get(
            f"/api/v1/vault/notes/{note_id}", headers=_headers(token)
        ).json()

        response = client.patch(
            f"/api/v1/vault/notes/{note_id}/metadata",
            headers=_headers(token),
            json={"base_revision": 1, "related_ids": ["d" * 32]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["re_embedded"] is False

        after = client.get(
            f"/api/v1/vault/notes/{note_id}", headers=_headers(token)
        ).json()
        assert after["related_ids"] == ["d" * 32]
        # Untouched fields, and the content in particular, are exactly as they were.
        assert after["body"] == before["body"]
        assert after["title"] == before["title"]
        assert after["tags"] == before["tags"]
        assert provider.calls == 0, (
            "PATCH /metadata spent an embedding call. Do not raise the "
            "expected count: it is the evidence that this path cannot alter "
            "retrieval, so changing it deletes the check rather than fixing "
            "the cause. See the identical assertion in "
            "test_review_accepts_a_metadata_proposal_without_touching_content "
            "for the two changes that produce this."
        )
    finally:
        _cleanup()
        _drop(credential_id)


def test_http_patch_refuses_a_stale_revision(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """The compare-and-set, over the transport that has no session to remember
    what the caller last read."""

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.UPDATE))
    note_id = _seed_note()
    _ensure_embedded(note_id, provider)
    try:
        first = client.patch(
            f"/api/v1/vault/notes/{note_id}/metadata",
            headers=_headers(token),
            json={"base_revision": 1, "related_ids": ["e" * 32]},
        )
        assert first.status_code == 200, first.text

        # The same caller retrying against the revision it originally read.
        stale = client.patch(
            f"/api/v1/vault/notes/{note_id}/metadata",
            headers=_headers(token),
            json={"base_revision": 1, "related_ids": ["f" * 32]},
        )

        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["retryable"] is True

        after = client.get(
            f"/api/v1/vault/notes/{note_id}", headers=_headers(token)
        ).json()
        assert after["related_ids"] == ["e" * 32], "the refused write changed nothing"
    finally:
        _cleanup()
        _drop(credential_id)


def test_http_patch_refuses_an_empty_change(
    client: TestClient, provider: StubEmbeddingProvider
) -> None:
    """A request that names no field is a caller mistake, not a no-op write."""

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.UPDATE))
    note_id = _seed_note()
    try:
        response = client.patch(
            f"/api/v1/vault/notes/{note_id}/metadata",
            headers=_headers(token),
            json={"base_revision": 1},
        )

        assert response.status_code == 422, response.text
    finally:
        _cleanup()
        _drop(credential_id)


def test_metadata_acceptance_works_with_no_embedding_provider(
    client: TestClient, tokens: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A providerless deployment is an ordinary state, not a broken one.

    The shared acceptance path requires a provider before it does anything, so
    a metadata proposal used to fail with 503 on a deployment configured
    lexical-only -- refusing a change that needs no embedding because a
    component it never touches was absent.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: None)
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": ["a" * 32]},
                "rationale": "Connect it, with no provider configured.",
            },
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )

        assert decision.status_code == 200, decision.text
        assert decision.json()["outcome"] == "accepted"
        assert decision.json()["target"]["related_ids"] == ["a" * 32]
    finally:
        _cleanup()


def test_metadata_acceptance_leaves_a_note_without_an_embedding_alone(
    client: TestClient, tokens: tuple[str, str, str], provider: StubEmbeddingProvider
) -> None:
    """A note with no vector must not acquire one from a metadata change.

    The seeded note has no embedding row, which is the state the shared path
    treated as "must embed". Doing so from a metadata acceptance writes a
    vector this change had no business creating -- the clearest possible
    violation of the claim that metadata cannot affect retrieval.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": ["b" * 32]},
                "rationale": "Connect a note that was never embedded.",
            },
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )

        assert decision.status_code == 200, decision.text
        assert decision.json()["target"]["related_ids"] == ["b" * 32]
        assert provider.calls == 0, (
            "A note with no embedding acquired one from a metadata change. Do "
            "not accept this by expecting a call: the vector would describe "
            "text this change never touched, and writing it is precisely the "
            "retrieval effect ADR 0036 says a metadata edit cannot have."
        )
        assert _embedding_row_count(note_id) == 0, (
            "A vector row was created by a metadata acceptance."
        )
    finally:
        _cleanup()


def test_metadata_acceptance_leaves_an_existing_vector_byte_for_byte(
    client: TestClient, tokens: tuple[str, str, str], provider: StubEmbeddingProvider
) -> None:
    """The third state: a note that does have a vector keeps exactly that one."""

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    _ensure_embedded(note_id, provider)
    before = _embedding_digest(note_id)
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "facets": {"area": ["gameplay"]}},
                "rationale": "Reclassify without re-indexing.",
            },
        )
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        decision = client.post(
            f"/api/v1/vault/amendment-proposals/{proposal_id}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )

        assert decision.status_code == 200, decision.text
        assert _embedding_digest(note_id) == before
        assert provider.calls == 0
    finally:
        _cleanup()


def _embedding_row_count(note_id: str) -> int:
    transactions, engine = vault_service()

    async def count() -> int:
        try:
            async with transactions.transaction() as connection:
                return (
                    await connection.execute(
                        select(func.count()).select_from(
                            vault_document_embeddings
                        ).where(vault_document_embeddings.c.document_id == note_id)
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(count())


def _embedding_digest(note_id: str) -> bytes | None:
    transactions, engine = vault_service()

    async def digest() -> bytes | None:
        try:
            async with transactions.transaction() as connection:
                return (
                    await connection.execute(
                        select(
                            vault_document_embeddings.c.embedded_text_sha256
                        ).where(vault_document_embeddings.c.document_id == note_id)
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(digest())


def test_rest_refuses_a_source_url_that_is_both_set_and_cleared(
    client: TestClient, tokens: tuple[str, str, str]
) -> None:
    """Refused at the boundary, and the stored URL is still there afterwards.

    The service used to prefer `clear_source_url` when both arrived, so this
    request returned 200 and deleted a URL the caller had just asked to
    replace. Asserting the note afterwards is the part that matters: a 422 with
    the row already modified would be a worse bug than the one being fixed.
    """

    proposer, _, _ = tokens
    credential_id, updater = _issue(scopes=(VaultScope.READ, VaultScope.UPDATE))
    note_id = _seed_note()
    try:
        contradictory = {
            "source_url": "https://example.invalid/new",
            "clear_source_url": True,
        }

        applied = client.patch(
            f"/api/v1/vault/notes/{note_id}/metadata",
            headers=_headers(updater),
            json={"base_revision": 1, **contradictory},
        )
        assert applied.status_code == 422, applied.text
        assert "contradictory" in applied.text

        proposed = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", **contradictory},
                "rationale": "Set it and clear it at the same time.",
            },
        )
        assert proposed.status_code == 422, proposed.text

        note = client.get(
            f"/api/v1/vault/notes/{note_id}", headers=_headers(updater)
        ).json()
        assert note["content_revision"] == 1, (
            "A refused metadata request still wrote to the note. The "
            "validation has to run before the update, not alongside it."
        )
    finally:
        _cleanup()
        _drop(credential_id)


def test_a_metadata_proposal_previews_its_edges_with_titles(
    client: TestClient, tokens: tuple[str, str, str]
) -> None:
    """The review surface has to describe a change that touches no body.

    `preview` summarizes a *body* change, and a metadata proposal has none by
    construction -- so it reported an empty diff, truthfully and uselessly, and
    a reviewer reading that pane learned nothing about a change that can rewire
    a note's whole position in the graph. That is the gap this covers.

    Titles rather than ids because the decision being made is "do these two
    notes belong together", and a 32-character id cannot be judged. If this
    fails on the titles, resolve them -- do not drop the assertion back to ids.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    neighbour_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": [neighbour_id]},
                "rationale": "Connect these two.",
            },
        )
        assert submitted.status_code == 200, submitted.text
        proposal_id = submitted.json()["proposal"]["proposal_id"]

        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )
        assert detail.status_code == 200, detail.text
        preview = detail.json()["metadata_preview"]

        assert preview is not None, (
            "A metadata proposal came back with no metadata preview. The body "
            "preview cannot describe this kind -- it has no body change -- so "
            "without this a reviewer sees an empty diff and nothing else."
        )
        assert preview["related_added"] == [
            {"id": neighbour_id, "title": "Shell transport constraints"}
        ]
        assert preview["related_removed"] == []
        assert preview["changes_nothing"] is False
        # Untouched fields report None rather than an empty value, so
        # "not mentioned" stays distinguishable from "set to empty".
        assert preview["facets_before"] is None
        assert preview["source_url_changed"] is False
    finally:
        _cleanup()


def test_a_metadata_preview_reports_an_edge_that_points_at_nothing(
    client: TestClient, tokens: tuple[str, str, str]
) -> None:
    """A dangling edge is exactly what a reviewer is there to catch.

    Edges are not existence-checked on write (ADR 0025) because a note may
    reference one that is archived or not yet written. That makes the reviewer
    the first person positioned to tell a legitimate forward reference from a
    typo, so the preview reports a null title rather than omitting the edge.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    missing = "0" * 32
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": [missing]},
                "rationale": "Point at a note that does not exist.",
            },
        )
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        )

        assert detail.json()["metadata_preview"]["related_added"] == [
            {"id": missing, "title": None}
        ]
    finally:
        _cleanup()


def test_a_body_proposal_has_no_metadata_preview(
    client: TestClient, tokens: tuple[str, str, str]
) -> None:
    """The two previews describe different kinds and must not both fire."""

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    try:
        submitted = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json=_proposal(note_id),
        )
        proposal_id = submitted.json()["proposal"]["proposal_id"]
        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{proposal_id}",
            headers=_headers(reviewer),
        ).json()

        assert detail["metadata_preview"] is None
        assert detail["preview"] is not None, (
            "A replacement lost its body preview. The metadata preview is an "
            "addition beside it, not a replacement for it."
        )
    finally:
        _cleanup()


def test_reordering_edges_is_not_reported_as_changing_nothing(
    client: TestClient, tokens: tuple[str, str, str]
) -> None:
    """A pure reorder is a write, and the preview has to say so.

    Additions and removals are set differences, so swapping [a, b] to [b, a]
    produces neither -- and `related_ids` is a stored ordered array, so
    accepting still rewrites the column and advances the revision. Reporting
    `changes_nothing` there invites a reviewer to wave a write through on the
    grounds that it is not one.

    Do not fix a failure here by making the comparison order-insensitive: that
    is the bug. Either the preview reports the reorder or the write path stops
    preserving order, and it does preserve it.
    """

    proposer, reviewer, _ = tokens
    note_id = _seed_note()
    first, second = _seed_note(), _seed_note()
    try:
        seeded = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 1,
                "change": {"kind": "metadata", "related_ids": [first, second]},
                "rationale": "Establish an order.",
            },
        )
        accepted = client.post(
            f"/api/v1/vault/amendment-proposals/{seeded.json()['proposal']['proposal_id']}/decision",
            headers=_headers(reviewer),
            json={"decision": "accepted"},
        )
        assert accepted.status_code == 200, accepted.text

        swapped = client.post(
            "/api/v1/vault/amendment-proposals",
            headers=_headers(proposer),
            json={
                "target_note_id": note_id,
                "base_revision": 2,
                "change": {"kind": "metadata", "related_ids": [second, first]},
                "rationale": "Same edges, opposite order.",
            },
        )
        assert swapped.status_code == 200, swapped.text
        detail = client.get(
            f"/api/v1/vault/amendment-proposals/{swapped.json()['proposal']['proposal_id']}",
            headers=_headers(reviewer),
        ).json()
        preview = detail["metadata_preview"]

        assert preview["related_added"] == []
        assert preview["related_removed"] == []
        assert preview["related_reordered"] is True
        assert preview["changes_nothing"] is False, (
            "A reorder was reported as changing nothing. It rewrites the "
            "stored ordered list and bumps the revision, so a reviewer told "
            "otherwise is being told something false about a write."
        )
    finally:
        _cleanup()
