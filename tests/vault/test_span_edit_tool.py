"""The span-edit tool over MCP, end to end.

`tests/vault/test_span_edit.py` covers the text handling without a database.
What can only be tested here is that the transport reaches the amendment
service correctly and that a span edit lands as an ordinary body-diff
proposal -- the property ADR 0033 rests on, since it is what lets the review
path, the storage shape and the compact-diff policy stay unchanged.
"""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.vault.auth import VaultScope
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.repository import VaultDocumentRepository
from app.vault.settings import vault_enabled
from app.vault.tables import vault_amendment_proposals, vault_documents
from tests.vault.test_mcp import _rpc
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="The vault MCP adapter is only mounted when VAULT_ENABLED is true",
)

PREFIX = "test-span-"

BODY = (
    "A guard that runs last cannot guard anything.\n"
    "\n"
    "The decorator wraps the endpoint, and the framework resolves dependencies\n"
    "before the endpoint is called, so the token is charged too late.\n"
    "\n"
    "The fix is a router-level dependency.\n"
)


@pytest.fixture
def note() -> str:
    """One note, seeded and removed, with its proposals cleared after."""

    note_id = f"{PREFIX}{uuid4().hex}"
    transactions, engine = vault_service()

    async def seed() -> None:
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
                    title="A guard that runs last cannot guard anything",
                    body=BODY,
                    contributed_by="test:span-fixture",
                    provenance={"fixture": True},
                ),
            )

    async def clear() -> None:
        async with transactions.transaction() as connection:
            # Proposals reference the note, so they go first.
            await connection.execute(
                delete(vault_amendment_proposals).where(
                    vault_amendment_proposals.c.target_document_id == note_id
                )
            )
            await connection.execute(
                delete(vault_documents).where(vault_documents.c.id == note_id)
            )

    try:
        asyncio.run(seed())
        yield note_id
    finally:
        asyncio.run(clear())
        asyncio.run(engine.dispose())


def _propose(client: TestClient, token: str, **arguments: object) -> dict:
    payload = _rpc(
        client,
        token,
        "tools/call",
        {"name": "vault_propose_note_span_edit", "arguments": arguments},
    )
    result = payload["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def propose_token() -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.PROPOSE))
    try:
        yield token
    finally:
        _drop(credential_id)


def test_a_span_edit_is_stored_as_an_ordinary_body_diff(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    """The property the design rests on.

    Nothing downstream learns that a span was involved: the change is a
    `body_diff`, so the review surface, the removal-acknowledgement rule and
    the compact-diff policy all apply without knowing this authoring form
    exists.
    """

    outcome = _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=1,
        expected_text="The fix is a router-level dependency.",
        replacement_text="The fix is a router-level dependency, solved first.",
        rationale="Name the ordering that makes the fix work.",
    )

    assert "error" not in outcome, outcome
    assert outcome["proposal"]["change_kind"] == "body_diff"
    assert outcome["proposal"]["state"] == "pending"


def test_the_server_writes_the_diff_so_the_caller_does_not(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    """A canonical unified diff reaches storage, with hunk arithmetic correct.

    The caller sent no patch syntax at all. Reading the stored change back is
    what proves the server produced a real diff rather than stashing the span.
    """

    _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=1,
        expected_text="runs last",
        replacement_text="runs first",
        rationale="Correct the claim.",
    )

    transactions, engine = vault_service()

    async def read() -> dict:
        async with transactions.transaction() as connection:
            row = (
                await connection.execute(
                    vault_amendment_proposals.select().where(
                        vault_amendment_proposals.c.target_document_id == note
                    )
                )
            ).mappings().one()
            return dict(row["change"])

    try:
        change = asyncio.run(read())
    finally:
        asyncio.run(engine.dispose())

    diff = change["body_diff"]
    assert diff.startswith("--- current-body")
    assert "@@" in diff
    assert "-A guard that runs last cannot guard anything." in diff
    assert "+A guard that runs first cannot guard anything." in diff


def test_an_ambiguous_span_is_refused_and_says_how_to_fix_it(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    """"the endpoint" appears twice, so the tool must not pick one."""

    outcome = _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=1,
        expected_text="the endpoint",
        replacement_text="the handler",
        rationale="Use the framework-neutral word.",
    )

    assert "error" in outcome
    assert "appears" in outcome["error"]
    assert "occurrence" in outcome["error"]


def test_occurrence_resolves_the_ambiguity(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    outcome = _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=1,
        expected_text="the endpoint",
        replacement_text="the handler",
        occurrence=2,
        rationale="Rename the second mention only.",
    )

    assert "error" not in outcome, outcome
    assert outcome["proposal"]["change_kind"] == "body_diff"


def test_an_absent_span_is_refused(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    outcome = _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=1,
        expected_text="text that is not in the note",
        replacement_text="anything",
        rationale="Should not land.",
    )

    assert "error" in outcome
    assert "does not appear" in outcome["error"]


def test_a_stale_base_revision_is_refused_before_the_span_is_resolved(
    client: TestClient,
    note: str,
    propose_token: str,
) -> None:
    """Staleness outranks span resolution.

    The span is meaningful only against the revision the caller read, so a
    caller working from an older one must be told that rather than having
    their text matched against a body they have not seen.
    """

    outcome = _propose(
        client,
        propose_token,
        note_id=note,
        base_revision=99,
        expected_text="runs last",
        replacement_text="runs first",
        rationale="Stale.",
    )

    assert "error" in outcome
    assert "changed" in outcome["error"]


def test_the_tool_needs_the_propose_scope(
    client: TestClient,
    note: str,
) -> None:
    """A read-only credential neither sees the tool nor may call it."""

    credential_id, token = _issue(scopes=(VaultScope.READ,))
    try:
        listing = _rpc(client, token, "tools/list")
        names = [tool["name"] for tool in listing["result"]["tools"]]
        assert "vault_propose_note_span_edit" not in names

        outcome = _propose(
            client,
            token,
            note_id=note,
            base_revision=1,
            expected_text="runs last",
            replacement_text="runs first",
            rationale="Should be refused.",
        )
    finally:
        _drop(credential_id)

    assert "error" in outcome
    assert VaultScope.PROPOSE in outcome["error"]
