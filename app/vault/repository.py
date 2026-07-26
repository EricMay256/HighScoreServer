"""Connection-injected SQLAlchemy Core repositories for vault persistence."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
    VaultDocument,
    VaultReviewCase,
)
from app.vault.tables import vault_documents, vault_review_cases


def _document_from_row(row: RowMapping) -> VaultDocument:
    return VaultDocument(
        id=row["id"],
        kind=DocumentKind(row["kind"]),
        status=DocumentStatus(row["status"]),
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        tags=tuple(row["tags"]),
        related_ids=tuple(row["related_ids"]),
        source_ids=tuple(row["source_ids"]),
        contributed_by=row["contributed_by"],
        source_url=row["source_url"],
        provenance=dict(row["provenance"]),
        schema_version=row["schema_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        embedding_model=row["embedding_model"],
        embedded_at=row["embedded_at"],
        compile_run_id=row["compile_run_id"],
        compiled_by=row["compiled_by"],
        compiled_at=row["compiled_at"],
    )


def _review_case_from_row(row: RowMapping) -> VaultReviewCase:
    return VaultReviewCase(
        id=row["id"],
        candidate_document_id=row["candidate_document_id"],
        state=ReviewState(row["state"]),
        reason=row["reason"],
        similar_documents=tuple(row["similar_documents"]),
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
    )


class VaultDocumentRepository:
    """Persistence operations for vault documents."""

    _domain_columns = (
        vault_documents.c.id,
        vault_documents.c.kind,
        vault_documents.c.status,
        vault_documents.c.title,
        vault_documents.c.summary,
        vault_documents.c.body,
        vault_documents.c.tags,
        vault_documents.c.related_ids,
        vault_documents.c.source_ids,
        vault_documents.c.contributed_by,
        vault_documents.c.source_url,
        vault_documents.c.provenance,
        vault_documents.c.schema_version,
        vault_documents.c.created_at,
        vault_documents.c.updated_at,
        vault_documents.c.embedding_model,
        vault_documents.c.embedded_at,
        vault_documents.c.compile_run_id,
        vault_documents.c.compiled_by,
        vault_documents.c.compiled_at,
    )

    async def insert(
        self,
        connection: AsyncConnection,
        document: NewVaultDocument,
    ) -> VaultDocument:
        statement = (
            insert(vault_documents)
            .values(
                id=document.id,
                kind=document.kind.value,
                status=document.status.value,
                title=document.title,
                summary=document.summary,
                body=document.body,
                tags=list(document.tags),
                related_ids=list(document.related_ids),
                source_ids=list(document.source_ids),
                contributed_by=document.contributed_by,
                source_url=document.source_url,
                provenance=document.provenance,
                schema_version=document.schema_version,
                embedding=(
                    list(document.embedding) if document.embedding is not None else None
                ),
                embedding_model=document.embedding_model,
                embedded_at=document.embedded_at,
                compile_run_id=document.compile_run_id,
                compiled_by=document.compiled_by,
                compiled_at=document.compiled_at,
            )
            .returning(*self._domain_columns)
        )
        result = await connection.execute(statement)
        return _document_from_row(result.mappings().one())

    async def get_by_id(
        self,
        connection: AsyncConnection,
        document_id: str,
    ) -> VaultDocument | None:
        statement = select(*self._domain_columns).where(
            vault_documents.c.id == document_id
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _document_from_row(row) if row is not None else None


class VaultReviewCaseRepository:
    """Persistence operations for near-duplicate review cases."""

    async def insert_pending(
        self,
        connection: AsyncConnection,
        *,
        candidate_document_id: str,
        reason: str,
        similar_documents: Sequence[Mapping[str, Any]],
        review_case_id: UUID | None = None,
    ) -> VaultReviewCase:
        statement = (
            insert(vault_review_cases)
            .values(
                id=review_case_id or uuid4(),
                candidate_document_id=candidate_document_id,
                state=ReviewState.PENDING.value,
                reason=reason,
                similar_documents=[dict(document) for document in similar_documents],
            )
            .returning(*vault_review_cases.c)
        )
        result = await connection.execute(statement)
        return _review_case_from_row(result.mappings().one())
