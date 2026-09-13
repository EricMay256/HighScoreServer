"""Recoverable deletion, restore, and the change feed (ADR 0052).

The properties a sync client depends on: a tombstone it can learn from, a resend
that is not a second deletion, no path back for a deleted id except an operator's
restore, and a feed that delivers every change in order -- and says so, with a
410, when it can no longer promise that.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.vault.auth import VaultScope
from app.vault.cursors import encode_change_cursor, encode_cursor
from app.vault.service import (
    DocumentNotFound,
    HumanNoteNotDeleted,
    HumanPathTaken,
    HumanRestoreRequest,
    VaultHumanNoteService,
)
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_audit_events,
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
CHANGES = "/api/v1/vault/human/changes"


def _run(work):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await work(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _with_service(work):
    async def exercise():
        transactions, engine = vault_service()
        try:
            return await work(VaultHumanNoteService(transactions))
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _cleanup(principal_id: str) -> None:
    """Everything this principal wrote -- including notes it has since deleted."""

    async def remove(connection):
        written = select(vault_human_revisions.c.document_id).where(
            vault_human_revisions.c.principal_id == principal_id
        )
        contributed = select(vault_documents.c.id).where(
            vault_documents.c.contributed_by == f"human:{principal_id}"
        )
        ids = {row[0] for row in await connection.execute(written)} | {
            row[0] for row in await connection.execute(contributed)
        }
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
        _cleanup(principal_id)
        _drop(credential_id)


@pytest.fixture
def operator(configure_test_env: None):
    yield from _credential(
        (
            VaultScope.READ,
            VaultScope.HUMAN_READ,
            VaultScope.HUMAN_WRITE,
            VaultScope.HUMAN_DELETE,
        )
    )


def _path(operator: dict, name: str, folder: str = "03 Projects") -> str:
    return f"Human/{folder}/b4-{operator['run']}-{name}.md"


def _create(client: TestClient, operator: dict, name: str, **fields) -> dict:
    payload = {
        "title": f"Deletable {name}",
        "body": f"The {name} note.",
        "vault_path": _path(operator, name),
        "operation_id": uuid4().hex,
        **fields,
    }
    response = client.post(NOTES, json=payload, headers=_auth(operator["token"]))
    assert response.status_code == 201, response.text
    return {**response.json(), "operation_id": payload["operation_id"], "payload": payload}


def _delete(client: TestClient, operator: dict, note_id: str, base: int):
    return client.delete(
        f"{NOTES}/{note_id}",
        params={"base_resource_revision": base},
        headers=_auth(operator["token"]),
    )


def _history(note_id: str) -> list[tuple[str, int]]:
    async def read(connection):
        result = await connection.execute(
            select(vault_human_revisions.c.operation, vault_human_revisions.c.resource_revision)
            .where(vault_human_revisions.c.document_id == note_id)
            .order_by(vault_human_revisions.c.id)
        )
        return [tuple(row) for row in result]

    return _run(read)


def _head(client: TestClient, operator: dict) -> str:
    response = client.get(f"{CHANGES}/head", headers=_auth(operator["token"]))
    assert response.status_code == 200, response.text
    return response.json()["cursor"]


def _feed(client: TestClient, operator: dict, after: str, **params):
    return client.get(
        CHANGES, params={"after": after, **params}, headers=_auth(operator["token"])
    )


# -------------------------------------------------------------------- scopes ----


def test_deletion_requires_human_delete(client: TestClient, configure_test_env: None) -> None:
    for writer in _credential((VaultScope.READ, VaultScope.HUMAN_READ, VaultScope.HUMAN_WRITE)):
        note = _create(client, writer, "undeletable")
        assert _delete(client, writer, note["note_id"], base=1).status_code == 403


def test_the_feed_requires_human_read(client: TestClient, configure_test_env: None) -> None:
    for reader in _credential((VaultScope.READ,)):
        assert client.get(CHANGES, headers=_auth(reader["token"])).status_code == 403
        assert client.get(f"{CHANGES}/head", headers=_auth(reader["token"])).status_code == 403


# ------------------------------------------------------------------ deletion ----


def test_a_delete_removes_the_note_and_leaves_a_tombstone(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "gone")

    deleted = _delete(client, operator, note["note_id"], base=1)

    assert deleted.status_code == 200, deleted.text
    tombstone = deleted.json()
    assert tombstone["note_id"] == note["note_id"]
    assert tombstone["vault_path"] == note["vault_path"]
    assert tombstone["resource_revision"] == 2
    assert client.get(f"{NOTES}/{note['note_id']}", headers=_auth(operator["token"])).status_code == 404
    assert _history(note["note_id"]) == [("create", 1), ("delete", 2)]


def test_a_resent_delete_returns_the_same_tombstone(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "twice")
    first = _delete(client, operator, note["note_id"], base=1)

    again = _delete(client, operator, note["note_id"], base=1)

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(_history(note["note_id"])) == 2


def test_a_stale_delete_is_refused_and_deletes_nothing(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "edited-underneath")
    edited = client.put(
        f"{NOTES}/{note['note_id']}",
        json={"title": "Changed", "body": "Changed since you looked.", "base_resource_revision": 1},
        headers=_auth(operator["token"]),
    )
    assert edited.status_code == 200

    stale = _delete(client, operator, note["note_id"], base=1)

    assert stale.status_code == 409
    assert stale.json()["detail"]["current_resource_revision"] == 2
    assert client.get(f"{NOTES}/{note['note_id']}", headers=_auth(operator["token"])).status_code == 200


def test_nothing_but_restore_brings_a_deleted_note_back(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "resurrect")
    assert _delete(client, operator, note["note_id"], base=1).status_code == 200
    headers = _auth(operator["token"])

    edit = client.put(
        f"{NOTES}/{note['note_id']}",
        json={"title": "Back", "body": "Uploading dirty local edits.", "base_resource_revision": 1},
        headers=headers,
    )
    move = client.post(
        f"{NOTES}/{note['note_id']}/move",
        json={"vault_path": _path(operator, "elsewhere"), "base_resource_revision": 1},
        headers=headers,
    )
    replayed_create = client.post(NOTES, json=note["payload"], headers=headers)

    assert (edit.status_code, move.status_code, replayed_create.status_code) == (404, 404, 404)
    assert _history(note["note_id"]) == [("create", 1), ("delete", 2)]


def test_deleting_an_unknown_note_is_not_found(client: TestClient, operator: dict) -> None:
    assert _delete(client, operator, uuid4().hex, base=1).status_code == 404


# ------------------------------------------------------------------- restore ----


def test_restore_brings_the_note_back_under_its_id_past_its_tombstone(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "restored", frontmatter={"VaultID": "kept"})
    headers = _auth(operator["token"])
    assert client.put(
        f"{NOTES}/{note['note_id']}",
        json={"title": "Final words", "body": "The version worth keeping.", "base_resource_revision": 1, "frontmatter": {"VaultID": "kept"}},
        headers=headers,
    ).status_code == 200
    assert _delete(client, operator, note["note_id"], base=2).status_code == 200

    outcome = _with_service(
        lambda service: service.restore(
            HumanRestoreRequest(
                document_id=note["note_id"],
                principal_id=operator["principal"],
                request_id=uuid4().hex,
            )
        )
    )

    assert outcome.document.id == note["note_id"]
    fetched = client.get(f"{NOTES}/{note['note_id']}", headers=headers).json()
    assert fetched["body"] == "The version worth keeping."
    assert fetched["frontmatter"] == {"VaultID": "kept"}
    assert (fetched["resource_revision"], fetched["content_revision"]) == (4, 2)
    assert fetched["created_at"] == note["created_at"]
    assert _history(note["note_id"]) == [("create", 1), ("edit", 2), ("delete", 3), ("restore", 4)]


def test_restore_refuses_a_live_note_an_unknown_one_and_a_taken_path(
    client: TestClient, operator: dict
) -> None:
    live = _create(client, operator, "live")
    doomed = _create(client, operator, "doomed")
    assert _delete(client, operator, doomed["note_id"], base=1).status_code == 200
    # Someone writes a new note where the deleted one used to be.
    headers = _auth(operator["token"])
    taker = client.post(
        NOTES,
        json={**doomed["payload"], "operation_id": uuid4().hex, "title": "Squatter"},
        headers=headers,
    )
    assert taker.status_code == 201

    def restore(note_id: str):
        return lambda service: service.restore(
            HumanRestoreRequest(document_id=note_id, principal_id=operator["principal"], request_id=uuid4().hex)
        )

    with pytest.raises(HumanNoteNotDeleted):
        _with_service(restore(live["note_id"]))
    with pytest.raises(DocumentNotFound):
        _with_service(restore(uuid4().hex))
    with pytest.raises(HumanPathTaken):
        _with_service(restore(doomed["note_id"]))


# ---------------------------------------------------------------------- feed ----


def test_the_feed_delivers_every_change_in_order_including_tombstones(
    client: TestClient, operator: dict
) -> None:
    head = _head(client, operator)
    first = _create(client, operator, "feed-a")
    assert client.put(
        f"{NOTES}/{first['note_id']}",
        json={"title": "A again", "body": "Edited.", "base_resource_revision": 1},
        headers=_auth(operator["token"]),
    ).status_code == 200
    second = _create(client, operator, "feed-b")
    assert _delete(client, operator, first["note_id"], base=2).status_code == 200

    response = _feed(client, operator, head)

    assert response.status_code == 200, response.text
    page = response.json()
    ours = {first["note_id"]: "a", second["note_id"]: "b"}
    delivered = [
        (ours[change["note_id"]], change["resource_revision"], change["change_kind"])
        for change in page["changes"]
        if change["note_id"] in ours
    ]
    assert delivered == [("a", 1, "upsert"), ("a", 2, "upsert"), ("b", 1, "upsert"), ("a", 3, "delete")]
    assert page["next_cursor"] == page["changes"][-1]["cursor"]

    caught_up = _feed(client, operator, page["next_cursor"]).json()
    assert caught_up["changes"] == []
    assert caught_up["next_cursor"] == page["next_cursor"]
    assert caught_up["has_more"] is False


def test_the_feed_pages_and_resumes_from_any_entry(
    client: TestClient, operator: dict
) -> None:
    head = _head(client, operator)
    notes = [_create(client, operator, f"page-{index}")["note_id"] for index in range(3)]

    seen: list[str] = []
    cursor = head
    for _ in range(50):
        page = _feed(client, operator, cursor, limit=1).json()
        seen.extend(change["note_id"] for change in page["changes"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break

    assert [note_id for note_id in seen if note_id in notes] == notes


def test_a_cursor_the_feed_no_longer_holds_is_gone(
    client: TestClient, operator: dict
) -> None:
    note = _create(client, operator, "anchor")
    entry = _feed(client, operator, _head(client, operator)).json()
    # The head sits on the create just written, so the feed past it is empty;
    # build the cursors from what the head names instead.
    head = _head(client, operator)
    assert entry["changes"] == []

    async def position_of(connection):
        return (
            await connection.execute(
                select(vault_human_changes.c.id).where(
                    vault_human_changes.c.document_id == note["note_id"]
                )
            )
        ).scalar_one()

    position = _run(position_of)
    assert _feed(client, operator, encode_change_cursor(position, 1, note["note_id"])).status_code == 200

    # The same position naming a different entry: a database that is not the
    # one that issued the cursor.
    assert _feed(client, operator, encode_change_cursor(position, 7, note["note_id"])).status_code == 410
    assert _feed(client, operator, encode_change_cursor(position + 10_000, 1, uuid4().hex)).status_code == 410
    # Malformed, or a cursor from another walk, is a bad request, not a lost feed.
    assert _feed(client, operator, "not-a-cursor").status_code == 422
    assert _feed(client, operator, encode_cursor("path", "Human/x.md", "id")).status_code == 422
    assert _feed(client, operator, head).status_code == 200
