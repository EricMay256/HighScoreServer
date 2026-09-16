"""Who may read a Human note, and which Agent workflows never see one (ADR 0050).

Two audiences, decided by scope rather than by anything a request can say:

- A credential holding ``vault:human-read`` reads the Human collection through
  ``/human/notes`` -- including notes ``ai_read`` withholds from agents -- and
  reads no Agent note there.
- An agent keeps the ordinary read surface, bounded by ``ai_read``, which may
  include a Human note the policy allows. What it never reaches is a Human note
  as an input to its own workflows: the dedup comparison and compile planning.

The fixture holds one note of each kind that matters: a Human note agents may
read, a Human note agents may not, and an Agent note.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.vault.auth import VaultScope
from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.domain import (
    DocumentCollection,
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
)
from app.vault.read_policy import is_readable_path
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
    VaultWikiPageRepository,
)
from app.vault.search import VaultSearchRepository
from app.vault.settings import vault_enabled
from app.vault.tables import vault_documents
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PREFIX = "test-human-reads-"

# name: (tree, collection)
FIXTURE_NOTES = {
    # `ai_read: allowed`, so agents may read it through the ordinary surface.
    "open": ("Human/03 Projects/", DocumentCollection.HUMAN),
    # `ai_read: forbidden`. Its operator reads it; no agent surface does.
    "hidden": ("Human/07 People/", DocumentCollection.HUMAN),
    "agent": ("Agent/notes/", DocumentCollection.AGENT),
}


@pytest.fixture
def notes(configure_test_env: None) -> dict[str, dict[str, str]]:
    run = uuid4().hex[:8]
    seeded = {
        name: {
            "id": f"{PREFIX}{run}-{name}",
            "path": f"{tree}{PREFIX}{run}-{name}.md",
        }
        for name, (tree, _collection) in FIXTURE_NOTES.items()
    }
    transactions, engine = vault_service()

    async def seed() -> None:
        async with transactions.transaction() as connection:
            for name, (_tree, collection) in FIXTURE_NOTES.items():
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=seeded[name]["id"],
                        kind=DocumentKind.NOTE,
                        vault_path=seeded[name]["path"],
                        status=DocumentStatus.ACTIVE,
                        doc_status="Active",
                        title=f"Human reads fixture {name} {run}",
                        body=f"The {name} note of run {run}.",
                        contributed_by=f"agent:{PREFIX}seed",
                        provenance={"fixture": True},
                        collection=collection,
                    ),
                )

    async def clear() -> None:
        async with transactions.transaction() as connection:
            # Embeddings cascade with their documents.
            await connection.execute(
                delete(vault_documents).where(
                    vault_documents.c.id.like(f"{PREFIX}{run}-%")
                )
            )

    try:
        asyncio.run(seed())
        yield seeded
    finally:
        asyncio.run(clear())
        asyncio.run(engine.dispose())


@pytest.fixture
def read_token(configure_test_env: None) -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ,))
    try:
        yield token
    finally:
        _drop(credential_id)


@pytest.fixture
def human_token(configure_test_env: None) -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.HUMAN_READ))
    try:
        yield token
    finally:
        _drop(credential_id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------- the Human surface ----


def test_the_human_surface_requires_human_read(
    client: TestClient, notes: dict, read_token: str
) -> None:
    hidden = notes["hidden"]["id"]

    assert client.get(f"/api/v1/vault/human/notes/{hidden}").status_code == 401
    # A genuine read credential is refused, not served less: the boundary is
    # the scope, and nothing an ordinary reader sends can widen it.
    assert (
        client.get(
            f"/api/v1/vault/human/notes/{hidden}", headers=_auth(read_token)
        ).status_code
        == 403
    )
    assert (
        client.get("/api/v1/vault/human/notes", headers=_auth(read_token)).status_code
        == 403
    )


def test_a_note_hidden_from_agents_is_readable_by_its_operator(
    client: TestClient, notes: dict, human_token: str, read_token: str
) -> None:
    hidden = notes["hidden"]
    assert not is_readable_path(hidden["path"])

    response = client.get(
        f"/api/v1/vault/human/notes/{hidden['id']}", headers=_auth(human_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["note_id"] == hidden["id"]
    assert body["vault_path"] == hidden["path"]
    assert body["resource_revision"] == 1

    agent_view = client.get(
        f"/api/v1/vault/notes/{hidden['id']}", headers=_auth(read_token)
    )
    assert agent_view.status_code == 404


def test_the_human_surface_names_no_agent_note(
    client: TestClient, notes: dict, human_token: str
) -> None:
    agent = notes["agent"]["id"]

    assert (
        client.get(
            f"/api/v1/vault/human/notes/{agent}", headers=_auth(human_token)
        ).status_code
        == 404
    )
    # The same credential holds `vault:read`, and the ordinary surface serves
    # the note: the 404 above is the audience, not an absent row.
    assert (
        client.get(f"/api/v1/vault/notes/{agent}", headers=_auth(human_token)).status_code
        == 200
    )


def test_agents_still_read_a_human_note_the_policy_allows(
    client: TestClient, notes: dict, read_token: str
) -> None:
    """ADR 0048: agents may read Human notes `ai_read` allows, and write none."""

    open_note = notes["open"]
    assert is_readable_path(open_note["path"])

    response = client.get(
        f"/api/v1/vault/notes/{open_note['id']}", headers=_auth(read_token)
    )
    assert response.status_code == 200
    # The agent read model does not publish the Human write token.
    assert "resource_revision" not in response.json()


def test_the_human_listing_is_the_human_collection(
    client: TestClient, notes: dict, human_token: str
) -> None:
    response = client.get(
        "/api/v1/vault/human/notes",
        params={"limit": 100},
        headers=_auth(human_token),
    )
    assert response.status_code == 200, response.text
    rows = {row["note_id"]: row for row in response.json()["notes"]}

    assert notes["open"]["id"] in rows
    assert notes["hidden"]["id"] in rows
    assert notes["agent"]["id"] not in rows
    assert rows[notes["hidden"]["id"]]["resource_revision"] == 1

    # A prefix outside the collection is an empty page, not a way in.
    agent_tree = client.get(
        "/api/v1/vault/human/notes",
        params={"path": "Agent/", "limit": 100},
        headers=_auth(human_token),
    )
    assert agent_tree.status_code == 200
    assert agent_tree.json()["notes"] == []


def test_the_human_listing_pages_without_repeating_or_skipping(
    client: TestClient, notes: dict, human_token: str
) -> None:
    wanted = {notes["open"]["id"], notes["hidden"]["id"]}
    seen: list[str] = []
    params: dict[str, object] = {"path": "Human/", "limit": 1}

    for _ in range(200):
        page = client.get(
            "/api/v1/vault/human/notes", params=params, headers=_auth(human_token)
        ).json()
        seen.extend(row["note_id"] for row in page["notes"])
        if not page["next_cursor"]:
            break
        params = {**params, "after": page["next_cursor"]}
    else:
        pytest.fail("the Human listing never reached its last page")

    # Path order: `Human/03 Projects/` walks before `Human/07 People/`.
    expected = [
        note["id"]
        for note in sorted((notes["open"], notes["hidden"]), key=lambda n: n["path"])
    ]
    assert [note_id for note_id in seen if note_id in wanted] == expected
    assert len(seen) == len(set(seen))


# ------------------------------------ Agent workflows never see Human notes ----


def test_dedup_compares_agent_notes_only(configure_test_env: None, notes: dict) -> None:
    """An identical vector on a readable Human note does not reach the gate."""

    profile_id = f"test/human-reads-{uuid4().hex[:8]}:{EMBEDDING_DIMENSIONS}"
    vector = (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)
    open_note, agent = notes["open"], notes["agent"]
    assert is_readable_path(open_note["path"])

    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                for note in (open_note, agent):
                    await VaultDocumentEmbeddingRepository().upsert(
                        connection,
                        DocumentEmbedding(
                            document_id=note["id"],
                            profile_id=profile_id,
                            vector=vector,
                        ),
                    )
                return await VaultSearchRepository().find_similar(
                    connection,
                    embedding=vector,
                    profile_id=profile_id,
                    limit=10,
                )
        finally:
            await engine.dispose()

    similars = asyncio.run(exercise())

    assert [candidate.note_id for candidate in similars] == [agent["id"]]


def test_compile_planning_sees_agent_notes_only(
    configure_test_env: None, notes: dict
) -> None:
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await VaultWikiPageRepository().note_states(connection)
        finally:
            await engine.dispose()

    states = asyncio.run(exercise())

    assert notes["agent"]["id"] in states
    assert notes["open"]["id"] not in states
    assert notes["hidden"]["id"] not in states
