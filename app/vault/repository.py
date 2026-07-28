"""Connection-injected SQLAlchemy Core repositories for vault persistence."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from .domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
    VaultDocument,
    VaultReviewCase,
)
from .tables import vault_document_embeddings, vault_documents, vault_review_cases


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
        compile_run_id=row["compile_run_id"],
        compiled_by=row["compiled_by"],
        compiled_at=row["compiled_at"],
    )


def _document_embedding_from_row(row: RowMapping) -> DocumentEmbedding:
    return DocumentEmbedding(
        document_id=row["document_id"],
        profile_id=row["profile_id"],
        # pgvector hands back a numpy array; the domain record holds plain floats.
        vector=tuple(float(value) for value in row["embedding"]),
        embedded_at=row["embedded_at"],
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


class VaultDocumentEmbeddingRepository:
    """Persistence operations for per-profile document embeddings."""

    _domain_columns = (
        vault_document_embeddings.c.document_id,
        vault_document_embeddings.c.profile_id,
        vault_document_embeddings.c.embedding,
        vault_document_embeddings.c.embedded_at,
    )

    async def upsert(
        self,
        connection: AsyncConnection,
        embedding: DocumentEmbedding,
    ) -> DocumentEmbedding:
        values: dict[str, Any] = {
            "document_id": embedding.document_id,
            "profile_id": embedding.profile_id,
            "embedding": list(embedding.vector),
        }
        if embedding.embedded_at is not None:
            values["embedded_at"] = embedding.embedded_at

        statement = pg_insert(vault_document_embeddings).values(**values)
        # Re-embedding a document under a profile it already has replaces the
        # vector instead of conflicting, so an embed job is safe to re-run.
        # EXCLUDED carries the column default when embedded_at was not supplied.
        statement = statement.on_conflict_do_update(
            constraint="vault_document_embeddings_pkey",
            set_={
                "embedding": statement.excluded.embedding,
                "embedded_at": statement.excluded.embedded_at,
            },
        ).returning(*self._domain_columns)
        result = await connection.execute(statement)
        return _document_embedding_from_row(result.mappings().one())

    async def get(
        self,
        connection: AsyncConnection,
        document_id: str,
        profile_id: str,
    ) -> DocumentEmbedding | None:
        statement = select(*self._domain_columns).where(
            vault_document_embeddings.c.document_id == document_id,
            vault_document_embeddings.c.profile_id == profile_id,
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _document_embedding_from_row(row) if row is not None else None


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
