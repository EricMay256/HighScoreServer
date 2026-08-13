"""Connection-injected SQLAlchemy Core repositories for vault persistence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from .auth import VaultCredential
from .domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    ReviewState,
    VaultDocument,
    VaultReviewCase,
)
from .read_policy import readable_path_predicate
from .tables import (
    vault_agent_credentials,
    vault_audit_events,
    vault_document_embeddings,
    vault_documents,
    vault_review_cases,
    vault_write_requests,
)


# The public projection of a document. Shared with the retrieval module so the
# two read paths cannot drift into returning different shapes.
DOCUMENT_DOMAIN_COLUMNS = (
    vault_documents.c.id,
    vault_documents.c.kind,
    vault_documents.c.doc_type,
    vault_documents.c.vault_path,
    vault_documents.c.status,
    vault_documents.c.doc_status,
    vault_documents.c.title,
    vault_documents.c.summary,
    vault_documents.c.body,
    vault_documents.c.tags,
    vault_documents.c.aliases,
    vault_documents.c.frontmatter,
    vault_documents.c.facets,
    vault_documents.c.source_sha256,
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


def document_from_row(row: RowMapping) -> VaultDocument:
    return VaultDocument(
        id=row["id"],
        kind=DocumentKind(row["kind"]),
        doc_type=row["doc_type"],
        vault_path=row["vault_path"],
        status=DocumentStatus(row["status"]),
        doc_status=row["doc_status"],
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        tags=tuple(row["tags"]),
        aliases=tuple(row["aliases"]),
        frontmatter=dict(row["frontmatter"]),
        facets={k: list(v) for k, v in dict(row["facets"]).items()},
        source_sha256=row["source_sha256"],
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
        text_sha256=row["embedded_text_sha256"],
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

    _domain_columns = DOCUMENT_DOMAIN_COLUMNS

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
                doc_type=document.doc_type,
                vault_path=document.vault_path,
                status=document.status.value,
                doc_status=document.doc_status,
                title=document.title,
                summary=document.summary,
                body=document.body,
                tags=list(document.tags),
                aliases=list(document.aliases),
                frontmatter=document.frontmatter,
                facets=document.facets,
                source_sha256=document.source_sha256,
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
        return document_from_row(result.mappings().one())

    async def get_by_id(
        self,
        connection: AsyncConnection,
        document_id: str,
        statuses: Sequence[DocumentStatus] | None = None,
        readable_only: bool = False,
    ) -> VaultDocument | None:
        """Fetch one document, optionally restricted to certain statuses.

        Unfiltered by default: which statuses a caller may see is a policy of
        that surface, not of persistence. Review tooling has to be able to load
        a flagged document precisely because it is flagged, so the restriction
        belongs at the caller. ``routes.py`` states the read surface's rule.

        ``readable_only`` applies the ``ai_read`` path policy, and defaults
        off for the same reason: review, export, and reconciliation tooling
        must be able to load a row the public read surface withholds.
        """

        statement = select(*self._domain_columns).where(
            vault_documents.c.id == document_id
        )
        if statuses is not None:
            statement = statement.where(
                vault_documents.c.status.in_([status.value for status in statuses])
            )
        if readable_only:
            statement = statement.where(readable_path_predicate())
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return document_from_row(row) if row is not None else None


class VaultDocumentEmbeddingRepository:
    """Persistence operations for per-profile document embeddings."""

    _domain_columns = (
        vault_document_embeddings.c.document_id,
        vault_document_embeddings.c.profile_id,
        vault_document_embeddings.c.embedding,
        vault_document_embeddings.c.embedded_at,
        vault_document_embeddings.c.embedded_text_sha256,
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
            "embedded_text_sha256": embedding.text_sha256,
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
                "embedded_text_sha256": (
                    statement.excluded.embedded_text_sha256
                ),
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


class VaultAgentCredentialRepository:
    """Lookup and last-used tracking for operator-issued agent credentials."""

    _columns = (
        vault_agent_credentials.c.id,
        vault_agent_credentials.c.principal_id,
        vault_agent_credentials.c.display_name,
        vault_agent_credentials.c.secret_sha256,
        vault_agent_credentials.c.scopes,
        vault_agent_credentials.c.created_at,
        vault_agent_credentials.c.expires_at,
        vault_agent_credentials.c.revoked_at,
        vault_agent_credentials.c.last_used_at,
    )

    async def get(
        self,
        connection: AsyncConnection,
        credential_id: str,
    ) -> VaultCredential | None:
        """Load a credential by its non-secret ID.

        Returns expired and revoked rows too. Whether a credential may be used
        is ``auth.authorize``'s decision, and filtering here would make a
        revoked credential indistinguishable from a nonexistent one in the
        logs — which is exactly the distinction an operator needs.
        """

        statement = select(*self._columns).where(
            vault_agent_credentials.c.id == credential_id
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return VaultCredential(
            id=row["id"],
            principal_id=row["principal_id"],
            display_name=row["display_name"],
            secret_sha256=bytes(row["secret_sha256"]),
            scopes=tuple(row["scopes"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            last_used_at=row["last_used_at"],
        )

    async def touch(
        self,
        connection: AsyncConnection,
        credential_id: str,
    ) -> None:
        """Record a successful use.

        Only on success: the column means "last used", not "last attempted", so
        a failed secret must not let an attacker keep a revoked-looking
        credential looking live.
        """

        await connection.execute(
            update(vault_agent_credentials)
            .where(vault_agent_credentials.c.id == credential_id)
            .values(last_used_at=func.now())
        )


@dataclass(frozen=True, slots=True)
class WriteRequestRecord:
    """A logical write, stable across retries of the same idempotency key."""

    principal_id: str
    idempotency_key: str
    request_sha256: bytes
    digest_version: int
    state: str
    document_id: str | None
    response: dict[str, Any] | None


class VaultWriteRequestRepository:
    """Idempotency records for governed writes."""

    _columns = (
        vault_write_requests.c.principal_id,
        vault_write_requests.c.idempotency_key,
        vault_write_requests.c.request_sha256,
        vault_write_requests.c.digest_version,
        vault_write_requests.c.state,
        vault_write_requests.c.document_id,
        vault_write_requests.c.response,
    )

    async def get(
        self,
        connection: AsyncConnection,
        principal_id: str,
        idempotency_key: str,
    ) -> WriteRequestRecord | None:
        statement = select(*self._columns).where(
            vault_write_requests.c.principal_id == principal_id,
            vault_write_requests.c.idempotency_key == idempotency_key,
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return WriteRequestRecord(
            principal_id=row["principal_id"],
            idempotency_key=row["idempotency_key"],
            request_sha256=bytes(row["request_sha256"]),
            digest_version=int(row["digest_version"]),
            state=row["state"],
            document_id=row["document_id"],
            response=dict(row["response"]) if row["response"] is not None else None,
        )

    async def complete(
        self,
        connection: AsyncConnection,
        *,
        principal_id: str,
        idempotency_key: str,
        request_sha256: bytes,
        digest_version: int,
        state: str,
        document_id: str | None,
        response: Mapping[str, Any],
    ) -> None:
        """Record the settled outcome of one logical write.

        Written inside the same transaction as the document, so a replay can
        never observe a document without its idempotency record or the reverse.

        ``request_sha256`` and ``digest_version`` are absent from the conflict
        update on purpose: they describe the request that first claimed the key,
        and a later settle of the same key must not quietly re-bless a different
        body as the canonical one. They are written together so a digest can
        never be read back without the rule that produced it.
        """

        statement = pg_insert(vault_write_requests).values(
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            digest_version=digest_version,
            state=state,
            document_id=document_id,
            response=dict(response),
            completed_at=func.now(),
        )
        await connection.execute(
            statement.on_conflict_do_update(
                constraint="vault_write_requests_pkey",
                set_={
                    "state": statement.excluded.state,
                    "document_id": statement.excluded.document_id,
                    "response": statement.excluded.response,
                    "completed_at": statement.excluded.completed_at,
                },
            )
        )


class VaultAuditEventRepository:
    """Append-only audit trail. See ADR 0002 for why it carries no foreign keys."""

    async def record(
        self,
        connection: AsyncConnection,
        *,
        operation: str,
        outcome: str,
        request_id: str,
        principal_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        await connection.execute(
            insert(vault_audit_events).values(
                principal_id=principal_id,
                operation=operation,
                target_type=target_type,
                target_id=target_id,
                idempotency_key=idempotency_key,
                outcome=outcome,
                request_id=request_id,
                trace_id=trace_id,
                latency_ms=latency_ms,
            )
        )
