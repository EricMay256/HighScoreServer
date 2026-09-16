"""Human create, edit and move: revision-checked, recorded, retry-safe (ADR 0051).

Through the HTTP surface, because the contract a sync client depends on is the
one the routes render: which status a stale base gets, what a resend returns,
and what the conflict body names. Snapshots and feed entries are then read
straight from their tables, since no endpoint serves them yet.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy import text as text_sql

from app.vault.auth import VaultScope
from app.vault.constants import CORPUS_LOCK_KEY
from app.vault.domain import (
    DocumentCollection,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
)
from app.vault.repository import VaultDocumentRepository, VaultHumanHistoryRepository
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_audit_events,
    vault_document_embeddings,
    vault_documents,
    vault_human_changes,
    vault_human_revisions,
    vault_write_requests,
)
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

NOTES = "/api/v1/vault/human/notes"
PREFIX = "test-human-writes-"


def _run(work):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await work(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _cleanup_principal(principal_id: str) -> None:
    async def remove(connection):
        ids = [
            row[0]
            for row in await connection.execute(
                select(vault_documents.c.id).where(
                    vault_documents.c.contributed_by == f"human:{principal_id}"
                )
            )
        ]
        # The ledger references documents, so it goes first.
        await connection.execute(
            delete(vault_write_requests).where(
                vault_write_requests.c.principal_id == principal_id
            )
        )
        await connection.execute(
            delete(vault_audit_events).where(
                vault_audit_events.c.principal_id == principal_id
            )
        )
        if ids:
            await connection.execute(
                delete(vault_human_changes).where(vault_human_changes.c.document_id.in_(ids))
            )
            await connection.execute(
                delete(vault_human_revisions).where(
                    vault_human_revisions.c.document_id.in_(ids)
                )
            )
            await connection.execute(
                delete(vault_documents).where(vault_documents.c.id.in_(ids))
            )

    _run(remove)


def _credential(scopes: tuple[str, ...]):
    credential_id, token = _issue(scopes=scopes)
    principal_id = f"test-principal-{credential_id}"
    try:
        yield {"token": token, "principal": principal_id, "run": uuid4().hex[:8]}
    finally:
        _cleanup_principal(principal_id)
        _drop(credential_id)


@pytest.fixture
def writer(configure_test_env: None):
    yield from _credential(
        (VaultScope.READ, VaultScope.HUMAN_READ, VaultScope.HUMAN_WRITE)
    )


def _path(writer: dict, name: str, folder: str = "03 Projects") -> str:
    return f"Human/{folder}/b3-{writer['run']}-{name}.md"


def _create(client: TestClient, writer: dict, path: str, **fields):
    payload = {
        "title": f"A Human note {writer['run']}",
        "body": "Written by a person.",
        "vault_path": path,
        "operation_id": uuid4().hex,
        **fields,
    }
    return client.post(NOTES, json=payload, headers=_auth(writer["token"]))


def _edit(client: TestClient, writer: dict, note_id: str, base: int, **fields):
    payload = {"title": "Retitled", "body": "Written again.", **fields}
    payload["base_resource_revision"] = base
    return client.put(f"{NOTES}/{note_id}", json=payload, headers=_auth(writer["token"]))


def _move(client: TestClient, writer: dict, note_id: str, path: str, base: int):
    return client.post(
        f"{NOTES}/{note_id}/move",
        json={"vault_path": path, "base_resource_revision": base},
        headers=_auth(writer["token"]),
    )


def _history(note_id: str) -> list[tuple[str, int]]:
    async def read(connection):
        result = await connection.execute(
            select(
                vault_human_revisions.c.operation,
                vault_human_revisions.c.resource_revision,
            )
            .where(vault_human_revisions.c.document_id == note_id)
            .order_by(vault_human_revisions.c.id)
        )
        return [tuple(row) for row in result]

    return _run(read)


def _changes(note_id: str) -> list[tuple[int, int, str, str]]:
    async def read(connection):
        result = await connection.execute(
            select(
                vault_human_changes.c.id,
                vault_human_changes.c.resource_revision,
                vault_human_changes.c.change_kind,
                vault_human_changes.c.vault_path,
            )
            .where(vault_human_changes.c.document_id == note_id)
            .order_by(vault_human_changes.c.id)
        )
        return [tuple(row) for row in result]

    return _run(read)


# ------------------------------------------------------------------ scopes ----


@pytest.mark.parametrize(
    "scopes",
    [
        (VaultScope.READ, VaultScope.HUMAN_READ),
        (VaultScope.READ, VaultScope.WRITE, VaultScope.UPDATE),
    ],
)
def test_human_writes_require_human_write(
    client: TestClient, configure_test_env: None, scopes: tuple[str, ...]
) -> None:
    for credential in _credential(scopes):
        response = _create(client, credential, _path(credential, "refused"))
        assert response.status_code == 403


# ------------------------------------------------------------------ create ----


def test_create_starts_at_revision_one_and_records_it(
    client: TestClient, writer: dict
) -> None:
    path = _path(writer, "created", folder="07 People")
    response = _create(
        client,
        writer,
        path,
        doc_type="Person",
        doc_status="Evergreen",
        frontmatter={"VaultID": "local-1", "Owner": ["me"]},
    )

    assert response.status_code == 201, response.text
    note = response.json()
    assert (note["resource_revision"], note["content_revision"]) == (1, 1)
    assert note["vault_path"] == path
    assert (note["doc_type"], note["doc_status"]) == ("Person", "Evergreen")
    assert note["frontmatter"] == {"VaultID": "local-1", "Owner": ["me"]}

    fetched = client.get(f"{NOTES}/{note['note_id']}", headers=_auth(writer["token"]))
    assert fetched.status_code == 200
    assert fetched.json()["frontmatter"] == note["frontmatter"]

    assert _history(note["note_id"]) == [("create", 1)]
    [(_, revision, kind, change_path)] = _changes(note["note_id"])
    assert (revision, kind, change_path) == (1, "upsert", path)

    async def embedded(connection):
        return (
            await connection.execute(
                select(vault_document_embeddings.c.document_id).where(
                    vault_document_embeddings.c.document_id == note["note_id"]
                )
            )
        ).first()

    # A Human save makes no embedding call; the daily job indexes it.
    assert _run(embedded) is None


def test_a_resent_create_returns_the_note_it_made(
    client: TestClient, writer: dict
) -> None:
    payload = {"operation_id": uuid4().hex}
    first = _create(client, writer, _path(writer, "resent"), **payload)
    second = _create(client, writer, _path(writer, "resent"), **payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["note_id"] == first.json()["note_id"]
    assert _history(first.json()["note_id"]) == [("create", 1)]


def test_an_operation_id_reused_for_a_different_body_is_refused(
    client: TestClient, writer: dict
) -> None:
    operation_id = uuid4().hex
    assert (
        _create(client, writer, _path(writer, "first"), operation_id=operation_id)
    ).status_code == 201

    reused = _create(client, writer, _path(writer, "second"), operation_id=operation_id)

    assert reused.status_code == 409


@pytest.mark.parametrize(
    "path",
    [
        "Agent/notes/not-human.md",
        "Human/03 Projects/not-markdown.txt",
        "Human/03 Projects/../escaped.md",
        "Human/.obsidian/config.md",
        "Human/03 Projects//empty-segment.md",
        "Human/03 Projects /trailing-space.md",
    ],
)
def test_create_refuses_a_path_that_cannot_name_a_human_note(
    client: TestClient, writer: dict, path: str
) -> None:
    assert _create(client, writer, path).status_code == 422


def test_create_refuses_a_path_taken_ignoring_case(
    client: TestClient, writer: dict
) -> None:
    path = _path(writer, "casing")
    assert _create(client, writer, path).status_code == 201

    shouted = path.replace(f"b3-{writer['run']}-casing", f"B3-{writer['run']}-CASING")
    assert _create(client, writer, shouted).status_code == 409


# -------------------------------------------------------------------- edit ----


def test_an_edit_on_the_current_revision_writes_one_new_revision(
    client: TestClient, writer: dict
) -> None:
    note = _create(client, writer, _path(writer, "edited")).json()

    edited = _edit(client, writer, note["note_id"], base=1, body="Second draft.")

    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["resource_revision"], body["content_revision"]) == (2, 2)
    assert body["body"] == "Second draft."
    assert _history(note["note_id"]) == [("create", 1), ("edit", 2)]
    positions = [position for position, *_ in _changes(note["note_id"])]
    assert positions == sorted(positions) and len(set(positions)) == 2


def test_a_stale_edit_is_a_conflict_that_names_the_current_revision(
    client: TestClient, writer: dict
) -> None:
    note = _create(client, writer, _path(writer, "stale")).json()
    assert _edit(client, writer, note["note_id"], base=1, body="Mine.").status_code == 200

    stale = _edit(client, writer, note["note_id"], base=1, body="Theirs, from an old base.")

    assert stale.status_code == 409
    assert stale.json()["detail"]["current_resource_revision"] == 2


def test_resending_an_applied_edit_succeeds_without_writing_again(
    client: TestClient, writer: dict
) -> None:
    note = _create(client, writer, _path(writer, "resend")).json()
    assert _edit(client, writer, note["note_id"], base=1, body="Once.").status_code == 200

    # The response was "lost", so the client resends against the base it had.
    resent = _edit(client, writer, note["note_id"], base=1, body="Once.")

    assert resent.status_code == 200
    assert resent.json()["resource_revision"] == 2
    assert _history(note["note_id"]) == [("create", 1), ("edit", 2)]


# -------------------------------------------------------------------- move ----


def test_a_move_changes_the_path_and_the_resource_revision_only(
    client: TestClient, writer: dict
) -> None:
    note = _create(client, writer, _path(writer, "before")).json()
    target = _path(writer, "after")

    moved = _move(client, writer, note["note_id"], target, base=1)

    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["vault_path"] == target
    assert (body["resource_revision"], body["content_revision"]) == (2, 1)
    assert body["updated_at"] == note["updated_at"]
    assert _history(note["note_id"]) == [("create", 1), ("move", 2)]
    assert _changes(note["note_id"])[-1][1:] == (2, "upsert", target)

    # Resending the move is the same no-op a resent edit is.
    assert _move(client, writer, note["note_id"], target, base=1).status_code == 200
    assert len(_history(note["note_id"])) == 2


def test_a_move_that_would_change_agent_readability_is_refused(
    client: TestClient, writer: dict
) -> None:
    note = _create(client, writer, _path(writer, "readable")).json()

    refused = _move(
        client, writer, note["note_id"], _path(writer, "hidden", folder="07 People"), base=1
    )

    assert refused.status_code == 422
    unchanged = client.get(f"{NOTES}/{note['note_id']}", headers=_auth(writer["token"]))
    assert unchanged.json()["vault_path"] == note["vault_path"]
    assert unchanged.json()["resource_revision"] == 1


def test_a_move_onto_a_path_taken_ignoring_case_is_refused(
    client: TestClient, writer: dict
) -> None:
    kept = _create(client, writer, _path(writer, "kept")).json()
    mover = _create(client, writer, _path(writer, "mover")).json()

    onto = kept["vault_path"].replace("kept", "KEPT")
    assert _move(client, writer, mover["note_id"], onto, base=1).status_code == 409


def test_a_stale_move_is_a_conflict(client: TestClient, writer: dict) -> None:
    note = _create(client, writer, _path(writer, "raced")).json()
    assert _edit(client, writer, note["note_id"], base=1, body="Edited first.").status_code == 200

    stale = _move(client, writer, note["note_id"], _path(writer, "raced-away"), base=1)

    assert stale.status_code == 409
    assert stale.json()["detail"]["current_resource_revision"] == 2


# ------------------------------------------------------------------ boundary ----


def test_human_writes_never_reach_an_agent_note(
    client: TestClient, writer: dict
) -> None:
    agent_id = f"{PREFIX}{writer['run']}-agent"

    async def seed(connection):
        await VaultDocumentRepository().insert(
            connection,
            NewVaultDocument(
                id=agent_id,
                kind=DocumentKind.NOTE,
                vault_path=f"Agent/notes/{agent_id}.md",
                status=DocumentStatus.ACTIVE,
                title="An agent's note",
                body="Written through the governed path.",
                contributed_by=f"agent:{PREFIX}seed",
                provenance={"fixture": True},
            ),
        )

    async def clear(connection):
        await connection.execute(delete(vault_documents).where(vault_documents.c.id == agent_id))

    _run(seed)
    try:
        assert _edit(client, writer, agent_id, base=1).status_code == 404
        assert (
            _move(client, writer, agent_id, _path(writer, "stolen"), base=1).status_code
            == 404
        )
    finally:
        _run(clear)


def test_history_refuses_to_record_without_the_corpus_lock(
    configure_test_env: None,
) -> None:
    """The feed's ordering depends on the lock, so the write checks for it."""

    class Rollback(Exception):
        pass

    document_id = f"{PREFIX}lock-{uuid4().hex}"
    history = VaultHumanHistoryRepository()

    async def attempt(connection):
        stored = await VaultDocumentRepository().insert(
            connection,
            NewVaultDocument(
                id=document_id,
                kind=DocumentKind.NOTE,
                vault_path=f"Human/03 Projects/{document_id}.md",
                status=DocumentStatus.ACTIVE,
                title="Lock fixture",
                body="Never committed.",
                contributed_by="human:test-lock",
                provenance={"fixture": True},
                collection=DocumentCollection.HUMAN,
            ),
        )
        arguments = {
            "operation": "create",
            "change_kind": "upsert",
            "principal_id": "test-lock",
            "request_id": "test-lock",
        }
        with pytest.raises(RuntimeError, match="corpus lock"):
            await history.record(connection, stored, **arguments)

        await connection.execute(
            text_sql("SELECT pg_advisory_xact_lock(:key)"), {"key": CORPUS_LOCK_KEY}
        )
        position = await history.record(connection, stored, **arguments)
        # Nothing here should outlive the test.
        raise Rollback(position)

    with pytest.raises(Rollback) as recorded:
        _run(attempt)
    assert isinstance(recorded.value.args[0], int)
