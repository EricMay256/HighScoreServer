"""Promotion candidacy, and the path move that projects it into a folder.

ADR 0023 makes candidacy a field and the folder a view of it. The behaviour
worth pinning is not that a column changed but that ``vault_path`` moved with
it, that the rendered file is byte-identical either side of the move, and that
the verb cannot relocate a row into a tree the service does not own.

Exercised against the database rather than a stand-in, because the two things
that can go wrong -- a UNIQUE collision on ``vault_path`` and an enum the
migration did not create -- are both facts about Postgres.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select

from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    PromotionStatus,
)
from app.vault.export import render_document
from app.vault.repository import VaultDocumentRepository
from app.vault.service import (
    AGENT_NOTES_DIRECTORY,
    AGENT_WIKI_DIRECTORY,
    PROMOTION_CANDIDATES_DIRECTORY,
    DocumentNotFound,
    PromotionNotApplicable,
    PromotionRequest,
    VaultPromotionService,
)
from app.vault.tables import (
    vault_audit_events,
    vault_compile_runs,
    vault_documents,
)
from tests.vault.test_search import vault_service


PRINCIPAL_PREFIX = "test-promotion-"


def _seed(
    title: str = "Worth promoting",
    *,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    kind: DocumentKind = DocumentKind.NOTE,
    vault_path: str | None = None,
    doc_type: str = "Agent Note",
) -> str:
    """Insert one document and return its id.

    ``vault_path`` defaults to the slug of the title under ``Agent/notes/``,
    which is what the contribute path would have assigned.
    """

    document_id = f"{PRINCIPAL_PREFIX}{uuid4().hex}"
    transactions, engine = vault_service()

    async def seed() -> None:
        try:
            async with transactions.transaction() as connection:
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=kind,
                        doc_type=doc_type,
                        vault_path=(
                            vault_path
                            if vault_path is not None
                            else f"{AGENT_NOTES_DIRECTORY}{document_id}.md"
                        ),
                        status=status,
                        doc_status=(
                            "Flagged"
                            if status is DocumentStatus.FLAGGED
                            else "Active"
                        ),
                        title=title,
                        body="A note somebody thought a human should read.",
                        contributed_by=f"agent:{PRINCIPAL_PREFIX}seed",
                        provenance={"fixture": True},
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    return document_id


def _cleanup() -> None:
    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.like(f"{PRINCIPAL_PREFIX}%")
                    )
                )
                await connection.execute(
                    delete(vault_compile_runs).where(
                        vault_compile_runs.c.compiler_principal_id.like(
                            f"{PRINCIPAL_PREFIX}%"
                        )
                    )
                )
                await connection.execute(
                    delete(vault_audit_events).where(
                        vault_audit_events.c.operation == "vault.promote"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())


@pytest.fixture(autouse=True)
def clean_promotion_fixtures(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _promote(
    document_id: str,
    promotion_status: PromotionStatus | None,
    principal_id: str = f"{PRINCIPAL_PREFIX}reviewer",
):
    transactions, engine = vault_service()

    async def run():
        try:
            return await VaultPromotionService(transactions).set_promotion_status(
                PromotionRequest(
                    document_id=document_id,
                    promotion_status=promotion_status,
                    principal_id=principal_id,
                    request_id=uuid4().hex,
                )
            )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _load(document_id: str):
    transactions, engine = vault_service()

    async def run():
        try:
            async with transactions.transaction() as connection:
                return await VaultDocumentRepository().get_by_id(
                    connection, document_id
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_marking_a_candidate_moves_the_path_into_the_promotion_folder() -> None:
    document_id = _seed(title="Worth promoting")

    outcome = _promote(document_id, PromotionStatus.CANDIDATE)

    assert outcome.moved is True
    assert outcome.document.promotion_status is PromotionStatus.CANDIDATE
    assert (
        outcome.document.vault_path
        == f"{PROMOTION_CANDIDATES_DIRECTORY}worth-promoting.md"
    )


def test_settling_a_candidacy_sends_the_note_home() -> None:
    """``promoted`` and ``retracted`` both export back to ``Agent/notes/``.

    Routing is binary; the field is three-valued so the outcome survives. A
    promoted note is not consumed -- promotion writes a new Human note rather
    than moving this one.
    """

    document_id = _seed(title="Worth promoting")
    _promote(document_id, PromotionStatus.CANDIDATE)

    outcome = _promote(document_id, PromotionStatus.PROMOTED)

    assert outcome.moved is True
    assert outcome.document.promotion_status is PromotionStatus.PROMOTED
    assert (
        outcome.document.vault_path == f"{AGENT_NOTES_DIRECTORY}worth-promoting.md"
    )


def test_a_settled_judgement_does_not_move_the_file_again() -> None:
    """``promoted`` and ``retracted`` share a folder, so only the field moves."""

    document_id = _seed(title="Worth promoting")
    _promote(document_id, PromotionStatus.CANDIDATE)
    _promote(document_id, PromotionStatus.PROMOTED)
    before = _load(document_id)

    outcome = _promote(document_id, PromotionStatus.RETRACTED)

    assert outcome.moved is False
    assert outcome.document.promotion_status is PromotionStatus.RETRACTED
    assert outcome.document.vault_path == before.vault_path


def test_re_applying_the_same_judgement_is_a_no_op() -> None:
    """Not an error, and specifically not a re-resolution of the path.

    Re-resolving would suffix the note against its own name -- ``-2`` -- and
    rewrite the file to say nothing new.
    """

    document_id = _seed(title="Worth promoting")
    first = _promote(document_id, PromotionStatus.CANDIDATE)

    second = _promote(document_id, PromotionStatus.CANDIDATE)

    assert second.moved is False
    assert second.document.vault_path == first.document.vault_path


def test_the_rendered_file_is_byte_identical_across_a_move() -> None:
    """What makes git show a rename and follow the note's history.

    ``updated_at`` deliberately does not move, so ``LastUpdated`` does not
    either; the only difference between the two projections is where the file
    lands. A bumped timestamp would turn every promotion into a rename plus an
    edit.
    """

    document_id = _seed(title="Worth promoting")
    before = render_document(_load(document_id))

    _promote(document_id, PromotionStatus.CANDIDATE)
    after = render_document(_load(document_id))

    assert after.vault_path != before.vault_path
    assert after.content == before.content


def test_a_collision_in_the_promotion_folder_suffixes_rather_than_failing() -> None:
    """``vault_path`` is UNIQUE and two notes may legitimately share a title.

    The dedup gate scores meaning, not titles, so this is an ordinary event
    that has to be resolved before the UPDATE rather than caught after it.
    """

    first = _seed(title="Worth promoting")
    second = _seed(title="Worth promoting")

    _promote(first, PromotionStatus.CANDIDATE)
    outcome = _promote(second, PromotionStatus.CANDIDATE)

    assert (
        outcome.document.vault_path
        == f"{PROMOTION_CANDIDATES_DIRECTORY}worth-promoting-2.md"
    )


def test_a_retracted_note_suffixes_when_its_old_name_was_taken() -> None:
    """Leaving ``Agent/notes/`` frees the name, and something may have taken it."""

    document_id = _seed(title="Worth promoting")
    _promote(document_id, PromotionStatus.CANDIDATE)
    # A second note claims the vacated slug while the first is away.
    squatter = _seed(
        title="Worth promoting",
        vault_path=f"{AGENT_NOTES_DIRECTORY}worth-promoting.md",
    )
    assert _load(squatter) is not None

    outcome = _promote(document_id, PromotionStatus.RETRACTED)

    assert (
        outcome.document.vault_path
        == f"{AGENT_NOTES_DIRECTORY}worth-promoting-2.md"
    )


def _seed_wiki_page(title: str = "Idempotency across the write path") -> str:
    """A compiled page, with the compile provenance its CHECK constraint wants.

    Nothing in the service writes one of these yet -- compilation is NEXT-STEPS
    item 5 -- but the promotion verb has to route a page home correctly the
    first time one exists, because ``Agent/notes/`` is typed to ``Agent Note``
    alone and a page landing there would violate the rule its own path selects.
    """

    document_id = f"{PRINCIPAL_PREFIX}{uuid4().hex}"
    run_id = uuid4()
    transactions, engine = vault_service()

    async def seed() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(
                    insert(vault_compile_runs).values(
                        id=run_id,
                        compiler_principal_id=f"{PRINCIPAL_PREFIX}librarian",
                    )
                )
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.WIKI,
                        doc_type="Wiki Page",
                        vault_path=f"{AGENT_WIKI_DIRECTORY}{document_id}.md",
                        status=DocumentStatus.ACTIVE,
                        doc_status="Current",
                        title=title,
                        body="Synthesized from several notes.",
                        contributed_by=f"agent:{PRINCIPAL_PREFIX}librarian",
                        provenance={"fixture": True},
                        compile_run_id=run_id,
                        compiled_by=f"agent:{PRINCIPAL_PREFIX}librarian",
                        compiled_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    return document_id


def test_a_wiki_page_returns_to_the_wiki_folder_not_the_notes_folder() -> None:
    """``folders.yml`` types ``Agent/notes/`` to ``Agent Note`` alone.

    ADR 0023 widened the candidates folder to both kinds, on the reasoning that
    a page distilling several notes is if anything more human-worthy than a raw
    one. So a page that goes in has to come back out somewhere legal, and home
    is keyed on ``kind`` rather than on where the row happens to be sitting.
    """

    document_id = _seed_wiki_page()

    candidate = _promote(document_id, PromotionStatus.CANDIDATE)
    assert candidate.document.vault_path == (
        f"{PROMOTION_CANDIDATES_DIRECTORY}idempotency-across-the-write-path.md"
    )

    retracted = _promote(document_id, PromotionStatus.RETRACTED)
    assert retracted.document.vault_path == (
        f"{AGENT_WIKI_DIRECTORY}idempotency-across-the-write-path.md"
    )


def test_a_document_outside_the_two_folders_is_refused() -> None:
    """The one way this verb could move a row into a tree it does not own."""

    document_id = _seed(
        title="An imported human note",
        vault_path=f"Human/06 Reference/{uuid4().hex}.md",
    )

    with pytest.raises(PromotionNotApplicable):
        _promote(document_id, PromotionStatus.CANDIDATE)


def test_a_flagged_note_cannot_be_promoted() -> None:
    """A candidate is served to agents and inside the dedup gate (ADR 0023).

    ``flagged`` is neither, so projecting one into a folder that means
    *elevated* would put a file in front of a librarian for content the read
    surface withholds. Accept its review case first.
    """

    document_id = _seed(title="Worth promoting", status=DocumentStatus.FLAGGED)

    with pytest.raises(DocumentNotFound):
        _promote(document_id, PromotionStatus.CANDIDATE)


def test_an_unknown_document_is_not_found() -> None:
    with pytest.raises(DocumentNotFound):
        _promote(f"{PRINCIPAL_PREFIX}{uuid4().hex}", PromotionStatus.CANDIDATE)


def test_clearing_the_judgement_returns_the_note_and_records_it() -> None:
    """``None`` is "never proposed"; ``retracted`` is "considered and declined".

    Both route home, and the audit event is what distinguishes a cleared field
    from one that was never set.
    """

    document_id = _seed(title="Worth promoting")
    _promote(document_id, PromotionStatus.CANDIDATE)

    outcome = _promote(document_id, None)

    assert outcome.document.promotion_status is None
    assert outcome.document.vault_path == f"{AGENT_NOTES_DIRECTORY}worth-promoting.md"
    assert _audit_outcomes() == ["candidate", "cleared"]


def _audit_outcomes() -> list[str]:
    transactions, engine = vault_service()

    async def run() -> list[str]:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(vault_audit_events.c.outcome)
                    .where(vault_audit_events.c.operation == "vault.promote")
                    .order_by(vault_audit_events.c.occurred_at)
                )
                return list(result.scalars())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_no_op_records_no_audit_event() -> None:
    """Nothing happened, so there is nothing for an incident to reconstruct."""

    document_id = _seed(title="Worth promoting")
    _promote(document_id, PromotionStatus.CANDIDATE)
    _promote(document_id, PromotionStatus.CANDIDATE)

    assert _audit_outcomes() == ["candidate"]


def test_promotion_leaves_content_and_timestamps_alone() -> None:
    document_id = _seed(title="Worth promoting")
    before = _load(document_id)

    _promote(document_id, PromotionStatus.CANDIDATE)
    after = _load(document_id)

    assert after.updated_at == before.updated_at
    assert after.body == before.body
    assert after.title == before.title
    assert after.status is before.status


def test_promotion_status_defaults_to_null_for_a_contributed_note() -> None:
    """Never proposed is the ordinary state, and the only one a write can create."""

    document_id = _seed(title="An ordinary note")

    assert _load(document_id).promotion_status is None
    assert _count_with_judgement() == 0


def _count_with_judgement() -> int:
    transactions, engine = vault_service()

    async def run() -> int:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(func.count())
                    .select_from(vault_documents)
                    .where(vault_documents.c.id.like(f"{PRINCIPAL_PREFIX}%"))
                    .where(vault_documents.c.promotion_status.isnot(None))
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())
