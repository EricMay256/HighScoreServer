import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.db import acquire_vault_connection, create_vault_engine
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
)
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
    VaultReviewCaseRepository,
)
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import (
    vault_document_embeddings,
    vault_documents,
    vault_review_cases,
)


def test_document_and_review_repositories_share_one_transaction(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        settings = replace(
            VaultSettings.from_environment(),
            enabled=True,
        )
        engine, observer = create_vault_engine(settings)
        service = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()
        reviews = VaultReviewCaseRepository()
        document_id = f"phase1-{uuid4().hex}"

        try:
            async with service.transaction() as connection:
                inserted = await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.NOTE,
                        status=DocumentStatus.FLAGGED,
                        title="Connection-injected repositories",
                        body="One service transaction supplies one connection.",
                        tags=("transactions", "postgres"),
                        contributed_by="test:phase1",
                        provenance={"fixture": True},
                    ),
                )
                review = await reviews.insert_pending(
                    connection,
                    candidate_document_id=document_id,
                    reason="Possible near-duplicate",
                    similar_documents=(
                        {
                            "note_id": "existing-note",
                            "title": "Existing",
                            "score": 0.9,
                        },
                    ),
                )

            assert inserted.id == document_id
            assert inserted.tags == ("transactions", "postgres")
            assert review.candidate_document_id == document_id
            assert review.state is ReviewState.PENDING

            async with service.transaction() as connection:
                loaded = await documents.get_by_id(connection, document_id)
                assert loaded == inserted
                await connection.execute(
                    delete(vault_review_cases).where(
                        vault_review_cases.c.candidate_document_id == document_id
                    )
                )
                await connection.execute(
                    delete(vault_documents).where(vault_documents.c.id == document_id)
                )
        finally:
            await engine.dispose()

        snapshot = observer.snapshot()
        assert snapshot.checked_out == 0
        assert snapshot.checkout_count == snapshot.checkin_count
        assert snapshot.checkout_count >= 2
        assert snapshot.latest_checkout_seconds is not None

    asyncio.run(exercise())


def test_service_exception_rolls_back_all_repository_writes(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        settings = replace(
            VaultSettings.from_environment(),
            enabled=True,
        )
        engine, observer = create_vault_engine(settings)
        service = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()
        reviews = VaultReviewCaseRepository()
        document_id = f"rollback-{uuid4().hex}"

        try:
            try:
                async with service.transaction() as connection:
                    await documents.insert(
                        connection,
                        NewVaultDocument(
                            id=document_id,
                            kind=DocumentKind.NOTE,
                            status=DocumentStatus.FLAGGED,
                            title="Rollback fixture",
                            body="Neither row should survive.",
                            contributed_by="test:phase1",
                            provenance={"fixture": True},
                        ),
                    )
                    await reviews.insert_pending(
                        connection,
                        candidate_document_id=document_id,
                        reason="Rollback assertion",
                        similar_documents=(),
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError as exc:
                assert str(exc) == "force rollback"

            async with service.transaction() as connection:
                assert await documents.get_by_id(connection, document_id) is None
        finally:
            await engine.dispose()

        assert observer.snapshot().checkout_failures == 0

    asyncio.run(exercise())


def _vector(seed: float) -> tuple[float, ...]:
    return tuple(seed + (index % 3) for index in range(EMBEDDING_DIMENSIONS))


def test_embeddings_are_per_profile_and_re_embedding_is_idempotent(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        settings = replace(VaultSettings.from_environment(), enabled=True)
        engine, observer = create_vault_engine(settings)
        service = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()
        embeddings = VaultDocumentEmbeddingRepository()
        document_id = f"embed-{uuid4().hex}"
        first_profile = "openai/text-embedding-3-small:1536"
        second_profile = "local/bge-small-en:1536"

        try:
            async with service.transaction() as connection:
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.NOTE,
                        status=DocumentStatus.ACTIVE,
                        title="Embeddings live beside documents",
                        body="One row per document per profile.",
                        contributed_by="test:phase1",
                        provenance={"fixture": True},
                    ),
                )

                # A document with no embedding row is simply not embedded.
                assert (
                    await embeddings.get(connection, document_id, first_profile)
                ) is None

                stored = await embeddings.upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=document_id,
                        profile_id=first_profile,
                        vector=_vector(0.5),
                    ),
                )
                assert stored.profile_id == first_profile
                assert len(stored.vector) == EMBEDDING_DIMENSIONS
                assert stored.embedded_at is not None

                # Re-embedding the same profile replaces the vector instead of
                # raising, so an embed job is safe to re-run.
                replaced = await embeddings.upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=document_id,
                        profile_id=first_profile,
                        vector=_vector(9.5),
                    ),
                )
                assert replaced.vector[0] == 9.5

                # A second profile coexists rather than displacing the first.
                other = await embeddings.upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=document_id,
                        profile_id=second_profile,
                        vector=_vector(2.5),
                    ),
                )

                assert (
                    await embeddings.get(connection, document_id, first_profile)
                ) == replaced
                assert (
                    await embeddings.get(connection, document_id, second_profile)
                ) == other

                row_count = await connection.scalar(
                    select(func.count())
                    .select_from(vault_document_embeddings)
                    .where(vault_document_embeddings.c.document_id == document_id)
                )
                assert row_count == 2

            async with service.transaction() as connection:
                # ON DELETE CASCADE: embeddings are derived data and do not
                # outlive their document.
                await connection.execute(
                    delete(vault_documents).where(vault_documents.c.id == document_id)
                )
                remaining = await connection.scalar(
                    select(func.count())
                    .select_from(vault_document_embeddings)
                    .where(vault_document_embeddings.c.document_id == document_id)
                )
                assert remaining == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_doc_type_round_trips_and_defaults_to_untyped(
    configure_test_env: None,
) -> None:
    """doc_type survives a write/read cycle, and absence means untyped.

    The Type Dictionary value is carried as text rather than an enum so the
    vocabulary can grow without a migration (ADR 0009); what the database
    guarantees is only that a present value is non-blank and bounded.
    """

    async def exercise() -> None:
        settings = replace(VaultSettings.from_environment(), enabled=True)
        engine, observer = create_vault_engine(settings)
        service = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()
        typed_id = f"doctype-{uuid4().hex}"
        untyped_id = f"doctype-{uuid4().hex}"

        try:
            async with service.transaction() as connection:
                typed = await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=typed_id,
                        kind=DocumentKind.NOTE,
                        # A real multi-word types.yml name: the shape
                        # constraint has to admit the interior space. "Agent
                        # Note" and "Wiki Page" are the two that actually reach
                        # this table today.
                        doc_type="Agent Note",
                        status=DocumentStatus.ACTIVE,
                        title="A typed note",
                        body="Carries a governance type.",
                        contributed_by="test:doc-type",
                        provenance={"fixture": True},
                    ),
                )
                untyped = await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=untyped_id,
                        kind=DocumentKind.NOTE,
                        status=DocumentStatus.ACTIVE,
                        title="An untyped note",
                        body="Carries no governance type.",
                        contributed_by="test:doc-type",
                        provenance={"fixture": True},
                    ),
                )

                assert typed.doc_type == "Agent Note"
                # Untyped is the absence of a value, not a sentinel string.
                assert untyped.doc_type is None

            async with service.transaction() as connection:
                reloaded = await documents.get_by_id(connection, typed_id)
                assert reloaded is not None
                assert reloaded.doc_type == "Agent Note"

                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.in_([typed_id, untyped_id])
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_database_rejects_malformed_doc_type_without_ruling_on_vocabulary(
    configure_test_env: None,
) -> None:
    """The CHECK constrains shape only; types.yml owns the vocabulary.

    An unknown-but-well-formed type is accepted by the database on purpose —
    rejecting it is application-level validation at the write boundary, which
    is what keeps adding a type out of the migration lineage.
    """

    async def exercise() -> None:
        settings = replace(VaultSettings.from_environment(), enabled=True)
        engine, observer = create_vault_engine(settings)
        service = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()

        def candidate(document_id: str, doc_type: str) -> NewVaultDocument:
            return NewVaultDocument(
                id=document_id,
                kind=DocumentKind.NOTE,
                doc_type=doc_type,
                status=DocumentStatus.ACTIVE,
                title="Shape check fixture",
                body="Only the shape of doc_type is enforced here.",
                contributed_by="test:doc-type",
                provenance={"fixture": True},
            )

        malformed = (
            "",
            "   ",
            " LeadingSpace",
            "Has\nNewline",
            "x" * 65,
        )

        try:
            for index, value in enumerate(malformed):
                document_id = f"doctype-bad-{index}-{uuid4().hex}"
                with pytest.raises(IntegrityError) as caught:
                    async with service.transaction() as connection:
                        await documents.insert(
                            connection,
                            candidate(document_id, value),
                        )
                assert "vault_documents_doc_type_format" in str(caught.value)

            # Well-formed but not in any Type Dictionary: the database's job
            # ends at shape, so this is stored rather than refused.
            unknown_id = f"doctype-unknown-{uuid4().hex}"
            async with service.transaction() as connection:
                stored = await documents.insert(
                    connection,
                    candidate(unknown_id, "Totally-Invented_Type"),
                )
                assert stored.doc_type == "Totally-Invented_Type"
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id == unknown_id
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_pool_size_one_queues_a_second_checkout(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        settings = replace(
            VaultSettings.from_environment(),
            enabled=True,
            pool_size=1,
            pool_timeout_seconds=2,
        )
        engine, observer = create_vault_engine(settings)

        async def acquire_once() -> None:
            async with acquire_vault_connection(engine, observer):
                return

        try:
            async with acquire_vault_connection(engine, observer):
                queued = asyncio.create_task(acquire_once())
                await asyncio.sleep(0.05)
                assert queued.done() is False
            await asyncio.wait_for(queued, timeout=1)
        finally:
            await engine.dispose()

        snapshot = observer.snapshot()
        assert snapshot.checkout_failures == 0
        assert snapshot.checkout_count == 2
        assert snapshot.checkin_count == 2

    asyncio.run(exercise())
