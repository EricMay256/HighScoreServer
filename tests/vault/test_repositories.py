import asyncio
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import delete

from app.vault.db import acquire_vault_connection, create_vault_engine
from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
)
from app.vault.repository import (
    VaultDocumentRepository,
    VaultReviewCaseRepository,
)
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_documents, vault_review_cases


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
