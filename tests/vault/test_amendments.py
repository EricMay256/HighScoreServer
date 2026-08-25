"""Revision-bound amendment proposals over the HTTP surface."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, update

from app.vault.auth import VaultScope
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.repository import VaultDocumentRepository
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_amendment_proposals,
    vault_audit_events,
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
