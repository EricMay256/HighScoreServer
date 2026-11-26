"""The Human collection boundary: which writer may change a row (vault ADR 0049).

Against the database, because every guarantee worth pinning here is one the
schema or a SQL predicate makes: the CHECKs that refuse a mixed scope set or a
cross-tree move, and the repository predicates that make a wrong-collection
target match nothing. No Human write path exists yet, so Human rows are
inserted directly -- they are exactly what an Agent path must fail to reach.

Human fixtures live under ``Human/03 Projects/``, which ``ai_read`` allows. A
path agents cannot read would let the Agent paths miss the row through the
read policy alone, and these tests would pass without the guard they exist to
prove.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError

from app.vault.auth import (
    VaultCredential,
    VaultScope,
    authorize,
    hash_secret,
    scopes_are_compatible,
)
from app.vault.domain import (
    DocumentCollection,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    PromotionStatus,
)
from app.vault.read_policy import is_readable_path
from app.vault.repository import VaultDocumentRepository, VaultWikiPageRepository
from app.vault.service import (
    DocumentNotFound,
    PromotionRequest,
    RetireRequest,
    VaultDocumentRetireService,
    VaultPromotionService,
)
from app.vault.tables import (
    vault_agent_credentials,
    vault_documents,
    vault_oauth_clients,
    vault_oauth_grants,
)
from tests.vault.test_search import vault_service


PREFIX = "test-human-boundary-"
HUMAN_TREE = "Human/03 Projects/"
AGENT_TREE = "Agent/notes/"


def _run(work):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await work(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _with_transactions(work):
    async def exercise():
        transactions, engine = vault_service()
        try:
            return await work(transactions)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _document(
    collection: DocumentCollection,
    *,
    vault_path: str | None = None,
) -> NewVaultDocument:
    document_id = f"{PREFIX}{uuid4().hex}"
    tree = HUMAN_TREE if collection is DocumentCollection.HUMAN else AGENT_TREE
    return NewVaultDocument(
        id=document_id,
        kind=DocumentKind.NOTE,
        vault_path=vault_path or f"{tree}{document_id}.md",
        status=DocumentStatus.ACTIVE,
        doc_status="Active",
        title=f"Boundary fixture {document_id}",
        body="A note one writer owns and the other must not reach.",
        contributed_by=f"agent:{PREFIX}seed",
        provenance={"fixture": True},
        collection=collection,
    )


def _insert(document: NewVaultDocument):
    return _run(lambda connection: VaultDocumentRepository().insert(connection, document))


def _load(document_id: str):
    return _run(
        lambda connection: VaultDocumentRepository().get_by_id(connection, document_id)
    )


def _insert_credential(scopes: tuple[str, ...]) -> None:
    async def create(connection):
        await connection.execute(
            insert(vault_agent_credentials).values(
                id=uuid4().hex[:16],
                principal_id=f"{PREFIX}{uuid4().hex[:8]}",
                display_name="boundary fixture",
                secret_sha256=hash_secret(uuid4().hex),
                scopes=sorted(scopes),
            )
        )

    _run(create)


def _insert_grant(authorized: list[str], entitled: list[str]) -> None:
    client_id = f"{PREFIX}{uuid4().hex}"

    async def create(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(client_id=client_id, client_info={})
        )
        await connection.execute(
            insert(vault_oauth_grants).values(
                family_id=uuid4(),
                client_id=client_id,
                authorized_scopes=authorized,
                entitled_scopes=entitled,
            )
        )

    _run(create)


def _cleanup() -> None:
    async def remove(connection):
        await connection.execute(
            delete(vault_documents).where(vault_documents.c.id.like(f"{PREFIX}%"))
        )
        await connection.execute(
            delete(vault_agent_credentials).where(
                vault_agent_credentials.c.principal_id.like(f"{PREFIX}%")
            )
        )
        # Grants cascade from their client.
        await connection.execute(
            delete(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id.like(f"{PREFIX}%")
            )
        )

    _run(remove)


@pytest.fixture
def clean(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


# ------------------------------------------------------------- scope rule ----


@pytest.mark.parametrize(
    ("scopes", "compatible"),
    [
        ((), True),
        ((VaultScope.READ, VaultScope.WRITE, VaultScope.PROPOSE), True),
        (
            (
                VaultScope.READ,
                VaultScope.HUMAN_READ,
                VaultScope.HUMAN_WRITE,
                VaultScope.HUMAN_DELETE,
            ),
            True,
        ),
        # Neither read nor export is a mutation, so neither takes a side.
        ((VaultScope.EXPORT, VaultScope.HUMAN_READ), True),
        # Reading private Human notes while able to write agent-readable ones
        # is the laundering channel, so Human read counts on the Human side.
        ((VaultScope.HUMAN_READ, VaultScope.WRITE), False),
        ((VaultScope.HUMAN_READ, VaultScope.PROPOSE), False),
        ((VaultScope.HUMAN_WRITE, VaultScope.UPDATE), False),
        ((VaultScope.HUMAN_DELETE, VaultScope.REVIEW), False),
        ((VaultScope.HUMAN_WRITE, VaultScope.COMPILE), False),
    ],
)
def test_human_and_agent_mutation_scopes_are_mutually_exclusive(
    scopes: tuple[str, ...], compatible: bool
) -> None:
    assert scopes_are_compatible(scopes) is compatible


def test_authorize_refuses_a_credential_that_carries_both_writers() -> None:
    secret = uuid4().hex

    def credential(*scopes: str) -> VaultCredential:
        return VaultCredential(
            id=uuid4().hex[:16],
            principal_id=f"{PREFIX}principal",
            display_name="boundary fixture",
            secret_sha256=hash_secret(secret),
            scopes=scopes,
            created_at=datetime.now(UTC),
        )

    # Refused even for an operation that needs only a scope it holds: a grant
    # nothing may hold authorizes nothing, rather than whatever it asks for.
    mixed = credential(VaultScope.READ, VaultScope.HUMAN_READ, VaultScope.WRITE)
    assert authorize(mixed, secret, (VaultScope.READ,)) == "incompatible"

    human = credential(VaultScope.READ, VaultScope.HUMAN_READ)
    assert authorize(human, secret, (VaultScope.READ,)) is None


# ---------------------------------------------------------- schema CHECKs ----


@pytest.mark.parametrize(
    "scopes",
    [
        (VaultScope.READ, VaultScope.HUMAN_READ, VaultScope.WRITE),
        (VaultScope.HUMAN_WRITE, VaultScope.UPDATE),
        (VaultScope.HUMAN_DELETE, VaultScope.REVIEW),
    ],
)
def test_the_schema_refuses_a_credential_holding_both_writers(
    clean: None, scopes: tuple[str, ...]
) -> None:
    with pytest.raises(IntegrityError, match="human_agent_exclusive"):
        _insert_credential(scopes)


def test_the_schema_accepts_a_credential_holding_only_the_human_side(
    clean: None,
) -> None:
    _insert_credential(
        (
            VaultScope.READ,
            VaultScope.HUMAN_READ,
            VaultScope.HUMAN_WRITE,
            VaultScope.HUMAN_DELETE,
        )
    )


def test_a_family_that_consented_to_write_cannot_be_entitled_to_human_scopes(
    clean: None,
) -> None:
    # The CHECK is over the union, because the union is what every rotation
    # projects onto the credential -- consent and entitlement are one grant.
    with pytest.raises(IntegrityError, match="vault_oauth_grants_human_agent_exclusive"):
        _insert_grant(
            [VaultScope.READ, VaultScope.WRITE, VaultScope.PROPOSE],
            [VaultScope.HUMAN_READ],
        )

    _insert_grant([VaultScope.READ], [VaultScope.HUMAN_READ, VaultScope.HUMAN_WRITE])


@pytest.mark.parametrize(
    ("collection", "tree"),
    [
        (DocumentCollection.AGENT, HUMAN_TREE),
        (DocumentCollection.HUMAN, AGENT_TREE),
    ],
)
def test_a_document_must_live_in_its_own_collections_tree(
    clean: None, collection: DocumentCollection, tree: str
) -> None:
    with pytest.raises(IntegrityError, match="collection_matches_path"):
        _insert(_document(collection, vault_path=f"{tree}{PREFIX}{uuid4().hex}.md"))


def test_moving_a_human_note_into_the_agent_tree_is_refused(clean: None) -> None:
    human = _insert(_document(DocumentCollection.HUMAN))

    async def move(connection):
        await connection.execute(
            update(vault_documents)
            .where(vault_documents.c.id == human.id)
            .values(vault_path=f"{AGENT_TREE}{human.id}.md")
        )

    with pytest.raises(IntegrityError, match="collection_matches_path"):
        _run(move)


def test_a_human_note_carries_no_agent_workflow_state(clean: None) -> None:
    human = _insert(_document(DocumentCollection.HUMAN))

    async def promote(connection):
        await connection.execute(
            update(vault_documents)
            .where(vault_documents.c.id == human.id)
            .values(promotion_status=PromotionStatus.CANDIDATE.value)
        )

    with pytest.raises(IntegrityError, match="human_has_no_agent_workflow"):
        _run(promote)


def test_content_revision_cannot_run_ahead_of_resource_revision(clean: None) -> None:
    agent = _insert(_document(DocumentCollection.AGENT))

    async def bump_content_only(connection):
        await connection.execute(
            update(vault_documents)
            .where(vault_documents.c.id == agent.id)
            .values(content_revision=vault_documents.c.content_revision + 1)
        )

    with pytest.raises(IntegrityError, match="resource_revision_covers_content"):
        _run(bump_content_only)


# ------------------------------------------------------- repository guard ----


def test_agent_mutations_match_no_human_row(clean: None) -> None:
    human = _insert(_document(DocumentCollection.HUMAN))
    documents = VaultDocumentRepository()

    async def attempt(connection):
        return {
            "replace_content": await documents.replace_content(
                connection, human.id, _document(DocumentCollection.HUMAN)
            ),
            "set_summary": await documents.set_summary(
                connection,
                human.id,
                summary="Agent-written summary",
                contributed_by=human.contributed_by,
                not_before=datetime(2000, 1, 1, tzinfo=UTC),
                expected_revision=human.content_revision,
            ),
            "set_status": await documents.set_status(
                connection,
                human.id,
                status=DocumentStatus.ARCHIVED,
                doc_status="Archived",
            ),
            "set_promotion_status": await documents.set_promotion_status(
                connection,
                human.id,
                promotion_status=PromotionStatus.CANDIDATE,
                vault_path=human.vault_path,
            ),
            "decline_notes": await VaultWikiPageRepository().decline_notes(
                connection, (human.id,), declined_at=datetime.now(UTC)
            ),
            "delete": await documents.delete(connection, human.id),
            "get_by_id as agent": await documents.get_by_id(
                connection, human.id, collection=DocumentCollection.AGENT
            ),
        }

    outcomes = _run(attempt)

    assert outcomes == {
        "replace_content": None,
        "set_summary": None,
        "set_status": None,
        "set_promotion_status": None,
        "decline_notes": (),
        "delete": False,
        "get_by_id as agent": None,
    }
    untouched = _load(human.id)
    assert untouched is not None
    assert untouched.status is DocumentStatus.ACTIVE
    assert (untouched.content_revision, untouched.resource_revision) == (1, 1)


def test_the_same_mutation_reaches_the_row_when_it_names_the_collection(
    clean: None,
) -> None:
    """The control for the test above: the predicate is what refused, not the row."""

    human = _insert(_document(DocumentCollection.HUMAN))

    archived = _run(
        lambda connection: VaultDocumentRepository().set_status(
            connection,
            human.id,
            status=DocumentStatus.ARCHIVED,
            doc_status="Archived",
            collection=DocumentCollection.HUMAN,
        )
    )

    assert archived is not None
    assert archived.collection is DocumentCollection.HUMAN
    assert archived.status is DocumentStatus.ARCHIVED


def test_resource_revision_moves_on_every_readable_change_content_revision_on_content(
    clean: None,
) -> None:
    agent = _insert(_document(DocumentCollection.AGENT))
    documents = VaultDocumentRepository()

    async def change(connection):
        archived = await documents.set_status(
            connection,
            agent.id,
            status=DocumentStatus.ARCHIVED,
            doc_status="Archived",
        )
        retitled = await documents.replace_content(
            connection,
            agent.id,
            replace(_document(DocumentCollection.AGENT), title="Retitled"),
        )
        return archived, retitled

    archived, retitled = _run(change)

    assert (archived.content_revision, archived.resource_revision) == (1, 2)
    assert (retitled.content_revision, retitled.resource_revision) == (2, 3)


# ---------------------------------------------------------- service guard ----


def _readable_human_note():
    human = _insert(_document(DocumentCollection.HUMAN))
    # Without this the tests below could pass on ADR 0014's read policy alone.
    assert is_readable_path(human.vault_path)
    return human


def test_retirement_cannot_reach_a_readable_human_note(clean: None) -> None:
    human = _readable_human_note()

    async def retire(transactions):
        await VaultDocumentRetireService(transactions).retire(
            RetireRequest(
                document_id=human.id,
                principal_id=f"{PREFIX}agent",
                request_id=uuid4().hex,
            )
        )

    with pytest.raises(DocumentNotFound):
        _with_transactions(retire)
    assert _load(human.id) is not None


def test_promotion_cannot_reach_a_readable_human_note(clean: None) -> None:
    human = _readable_human_note()

    async def promote(transactions):
        await VaultPromotionService(transactions).set_promotion_status(
            PromotionRequest(
                document_id=human.id,
                promotion_status=PromotionStatus.CANDIDATE,
                principal_id=f"{PREFIX}reviewer",
                request_id=uuid4().hex,
            )
        )

    with pytest.raises(DocumentNotFound):
        _with_transactions(promote)
    unchanged = _load(human.id)
    assert unchanged is not None
    assert unchanged.promotion_status is None
