"""Adjudicating the near-duplicate cases the write path opens.

The behaviour worth pinning is not "a field changed" but what a decision *does*
to the note: accepting publishes it, rejecting destroys it. Both are exercised
end to end over HTTP, because the scope gate and the status transition are the
two halves that have to agree.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.vault.auth import VaultScope
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument, ReviewState
from app.vault.repository import VaultDocumentRepository, VaultReviewCaseRepository
from app.vault.settings import vault_enabled
from app.vault.tables import vault_audit_events, vault_documents, vault_review_cases
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PRINCIPAL_PREFIX = "test-review-"


@pytest.fixture
def review_token(configure_test_env: None) -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.REVIEW))
    try:
        yield token
    finally:
        _drop(credential_id)


@pytest.fixture
def read_only_token(configure_test_env: None) -> str:
    """Deliberately without ``vault:review``.

    The review surface is the only one that serves ``flagged`` content, so the
    scope is what stands between an ordinary reader and the least-vetted text in
    the corpus.
    """

    credential_id, token = _issue(scopes=(VaultScope.READ,))
    try:
        yield token
    finally:
        _drop(credential_id)


def _seed_flagged_case(reason: str = "possible duplicate") -> tuple[str, str]:
    """Insert a flagged note and a pending case for it. Returns (note_id, case_id)."""

    note_id = f"{PRINCIPAL_PREFIX}{uuid4().hex}"
    transactions, engine = vault_service()

    async def seed() -> str:
        try:
            async with transactions.transaction() as connection:
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=note_id,
                        kind=DocumentKind.NOTE,
                        doc_type="Agent Note",
                        vault_path=f"Agent/notes/{note_id}.md",
                        status=DocumentStatus.FLAGGED,
                        doc_status="Flagged",
                        title="A flagged candidate awaiting judgement",
                        body="Written by the contribute path's Flag branch.",
                        contributed_by=f"agent:{PRINCIPAL_PREFIX}seed",
                        provenance={"fixture": True},
                    ),
                )
                case = await VaultReviewCaseRepository().insert_pending(
                    connection,
                    candidate_document_id=note_id,
                    reason=reason,
                    similar_documents=[
                        {"note_id": "other", "title": "The original", "score": 1.0}
                    ],
                )
                return str(case.id)
        finally:
            await engine.dispose()

    return note_id, asyncio.run(seed())


def _cleanup() -> None:
    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                orphans = (
                    select(vault_documents.c.id)
                    .where(vault_documents.c.id.like(f"{PRINCIPAL_PREFIX}%"))
                    .scalar_subquery()
                )
                await connection.execute(
                    delete(vault_review_cases).where(
                        vault_review_cases.c.candidate_document_id.in_(orphans)
                    )
                )
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.like(f"{PRINCIPAL_PREFIX}%")
                    )
                )
                await connection.execute(
                    delete(vault_audit_events).where(
                        vault_audit_events.c.operation == "vault.review"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())


def _count(table, **where) -> int:
    transactions, engine = vault_service()

    async def run() -> int:
        try:
            async with transactions.transaction() as connection:
                statement = select(func.count()).select_from(table)
                for column, value in where.items():
                    statement = statement.where(table.c[column] == value)
                return int((await connection.execute(statement)).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_the_queue_needs_the_review_scope(
    client: TestClient, read_only_token: str
) -> None:
    response = client.get(
        "/api/v1/vault/reviews",
        headers={"Authorization": f"Bearer {read_only_token}"},
    )

    assert response.status_code == 403, response.text


def test_reading_a_case_needs_the_review_scope(
    client: TestClient, read_only_token: str
) -> None:
    _, case_id = _seed_flagged_case()
    try:
        response = client.get(
            f"/api/v1/vault/reviews/{case_id}",
            headers={"Authorization": f"Bearer {read_only_token}"},
        )
        assert response.status_code == 403, response.text
    finally:
        _cleanup()


def test_the_queue_lists_pending_cases(
    client: TestClient, review_token: str
) -> None:
    note_id, case_id = _seed_flagged_case(reason="looks like an existing note")
    try:
        response = client.get(
            "/api/v1/vault/reviews",
            headers={"Authorization": f"Bearer {review_token}"},
            params={"limit": 200},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        mine = [c for c in payload["pending"] if c["review_case_id"] == case_id]
        assert len(mine) == 1
        case = mine[0]
        assert case["candidate_note_id"] == note_id
        assert case["state"] == "pending"
        assert case["reason"] == "looks like an existing note"
        assert case["similar"][0]["title"] == "The original"
        assert case["decided_at"] is None
    finally:
        _cleanup()


def test_a_case_serves_the_flagged_note_the_read_surface_withholds(
    client: TestClient, review_token: str
) -> None:
    """ADR 0008 hides flagged content from readers; a reviewer is the exception.

    Adjudicating a note you cannot read is not a review, so this surface -- and
    only this surface -- resolves it.
    """

    note_id, case_id = _seed_flagged_case()
    headers = {"Authorization": f"Bearer {review_token}"}
    try:
        # The ordinary read surface still refuses it.
        assert client.get(f"/api/v1/vault/notes/{note_id}", headers=headers).status_code == 404

        response = client.get(f"/api/v1/vault/reviews/{case_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["candidate"]["note_id"] == note_id
        assert payload["candidate"]["status"] == "flagged"
        assert "Flag branch" in payload["candidate"]["body"]
    finally:
        _cleanup()


def test_accepting_publishes_the_note(
    client: TestClient, review_token: str
) -> None:
    note_id, case_id = _seed_flagged_case()
    headers = {"Authorization": f"Bearer {review_token}"}
    try:
        response = client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers=headers,
            json={"decision": "accepted", "decision_note": "distinct enough"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["candidate"] == "published"
        assert payload["review_case"]["state"] == "accepted"
        assert payload["review_case"]["decision_note"] == "distinct enough"
        # From the credential, never the body.
        assert payload["review_case"]["decided_by"].startswith("agent:")

        # It now resolves through the ordinary read surface, and both status
        # fields moved together (ADR 0011 keeps them separate concepts).
        detail = client.get(f"/api/v1/vault/notes/{note_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "active"
        assert detail.json()["doc_status"] == "Active"
    finally:
        _cleanup()


def test_rejecting_deletes_the_note_and_keeps_the_judgement(
    client: TestClient, review_token: str
) -> None:
    """The decision migration 0011 made possible.

    A candidate is always a brand-new note, so a duplicate judged redundant at
    birth has no history to preserve -- ADR 0019 deletes what is wrong. The case
    survives with a null candidate rather than going with it.
    """

    note_id, case_id = _seed_flagged_case()
    headers = {"Authorization": f"Bearer {review_token}"}
    try:
        response = client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers=headers,
            json={"decision": "rejected"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["candidate"] == "deleted"
        assert payload["review_case"]["state"] == "rejected"
        assert payload["review_case"]["candidate_note_id"] is None

        assert _count(vault_documents, id=note_id) == 0
        # The judgement is still there, and still names why.
        assert _count(vault_review_cases, candidate_document_id=note_id) == 0
        assert _count(vault_review_cases, state="rejected") >= 1
    finally:
        _cleanup()


def test_deciding_a_settled_case_conflicts_rather_than_overwriting(
    client: TestClient, review_token: str
) -> None:
    """Two reviewers must not both believe they won.

    Each decision also moves the document, so a silent second write would
    publish a note the first reviewer deleted, or the reverse.
    """

    _, case_id = _seed_flagged_case()
    headers = {"Authorization": f"Bearer {review_token}"}
    try:
        first = client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers=headers,
            json={"decision": "accepted"},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers=headers,
            json={"decision": "rejected"},
        )
        assert second.status_code == 409, second.text
        # And the first decision stands.
        assert _count(vault_review_cases, state="accepted") >= 1
    finally:
        _cleanup()


def test_an_unknown_case_is_a_404(client: TestClient, review_token: str) -> None:
    headers = {"Authorization": f"Bearer {review_token}"}
    missing = uuid4()

    assert client.get(f"/api/v1/vault/reviews/{missing}", headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/v1/vault/reviews/{missing}/decision",
            headers=headers,
            json={"decision": "accepted"},
        ).status_code
        == 404
    )


def test_superseded_is_reserved_and_not_accepted(
    client: TestClient, review_token: str
) -> None:
    """The enum carries it; no decision path sets it.

    Leaving it unreachable is deliberate: an enum value with invented semantics
    is what this whole flow exists to correct.
    """

    _, case_id = _seed_flagged_case()
    try:
        response = client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers={"Authorization": f"Bearer {review_token}"},
            json={"decision": "superseded"},
        )
        assert response.status_code == 422, response.text
        assert ReviewState.SUPERSEDED.value == "superseded"
    finally:
        _cleanup()


def test_a_decision_leaves_an_audit_event(
    client: TestClient, review_token: str
) -> None:
    _, case_id = _seed_flagged_case()
    try:
        client.post(
            f"/api/v1/vault/reviews/{case_id}/decision",
            headers={"Authorization": f"Bearer {review_token}"},
            json={"decision": "accepted"},
        )
        assert _count(vault_audit_events, operation="vault.review") >= 1
    finally:
        _cleanup()
