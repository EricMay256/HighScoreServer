"""Connection-injected SQLAlchemy Core repositories for vault persistence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from .auth import VaultCredential
from .constants import (
    AUTHORIZATION_CODE_TTL_SECONDS,
    PENDING_AUTHORIZATION_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
)
from .domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    PendingAuthorization,
    PromotionStatus,
    RegisteredOAuthClient,
    ReviewState,
    StoredAuthorizationCode,
    StoredRefreshToken,
    VaultDocument,
    VaultReviewCase,
)
from .read_policy import readable_path_predicate
from .tables import (
    vault_agent_credentials,
    vault_audit_events,
    vault_document_embeddings,
    vault_documents,
    vault_oauth_authorization_codes,
    vault_oauth_clients,
    vault_oauth_pending_authorizations,
    vault_oauth_refresh_tokens,
    vault_review_cases,
    vault_write_requests,
)


# How coarse `last_used_at` is allowed to be. See VaultAgentCredentialRepository
# .touch: this is a write-amplification control, not a precision setting, so it
# is a constant rather than configuration -- a deployment has no reason to want
# a different answer, and tuning it down is how the write returns.
TOUCH_RESOLUTION = timedelta(seconds=60)

# The public projection of a document. Shared with the retrieval module so the
# two read paths cannot drift into returning different shapes.
DOCUMENT_DOMAIN_COLUMNS = (
    vault_documents.c.id,
    vault_documents.c.kind,
    vault_documents.c.doc_type,
    vault_documents.c.vault_path,
    vault_documents.c.status,
    vault_documents.c.doc_status,
    vault_documents.c.promotion_status,
    vault_documents.c.title,
    vault_documents.c.summary,
    vault_documents.c.body,
    vault_documents.c.tags,
    vault_documents.c.aliases,
    vault_documents.c.frontmatter,
    vault_documents.c.facets,
    vault_documents.c.origin,
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
        promotion_status=(
            None
            if row["promotion_status"] is None
            else PromotionStatus(row["promotion_status"])
        ),
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        tags=tuple(row["tags"]),
        aliases=tuple(row["aliases"]),
        frontmatter=dict(row["frontmatter"]),
        facets={k: list(v) for k, v in dict(row["facets"]).items()},
        origin=dict(row["origin"]),
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
                origin=document.origin,
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

    async def replace_content(
        self,
        connection: AsyncConnection,
        document_id: str,
        content: NewVaultDocument,
    ) -> VaultDocument | None:
        """Replace one document's caller-supplied content in place.

        Only the fields a caller supplies move. Identity, path, kind, governance
        type, status, contributor, and compile provenance are the service's or
        the corpus's, not the editor's -- an update is a new body for an
        existing row, not a new row wearing its id. ``contributed_by`` in
        particular stays put: overwriting it with the editor would erase who
        wrote the note, and who edited it is what the audit event records.

        Returns None when no row matched, so the caller can 404 without a
        separate existence check.
        """

        statement = (
            update(vault_documents)
            .where(vault_documents.c.id == document_id)
            .values(
                title=content.title,
                summary=content.summary,
                body=content.body,
                tags=list(content.tags),
                aliases=list(content.aliases),
                facets=content.facets,
                related_ids=list(content.related_ids),
                source_ids=list(content.source_ids),
                source_url=content.source_url,
                updated_at=func.now(),
            )
            .returning(*self._domain_columns)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return document_from_row(row) if row is not None else None

    async def set_status(
        self,
        connection: AsyncConnection,
        document_id: str,
        *,
        status: DocumentStatus,
        doc_status: str | None,
    ) -> VaultDocument | None:
        """Move a document's visibility state and its Status Map value together.

        The two are different things (ADR 0011) and neither derives from the
        other, which is precisely why they move in one statement: a review
        decision changes both, and leaving one behind would publish a note
        still labelled ``Flagged`` or label an unpublished one ``Active``.

        Content is untouched -- this is not a small ``replace_content``, and
        ``updated_at`` deliberately does not move: adjudicating a note is not
        editing it, and the export would otherwise churn every reviewed file.
        """

        statement = (
            update(vault_documents)
            .where(vault_documents.c.id == document_id)
            .values(status=status.value, doc_status=doc_status)
            .returning(*self._domain_columns)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return document_from_row(row) if row is not None else None

    async def set_promotion_status(
        self,
        connection: AsyncConnection,
        document_id: str,
        *,
        promotion_status: PromotionStatus | None,
        vault_path: str,
    ) -> VaultDocument | None:
        """Move candidacy and the path it routes to, in one statement.

        The folder is a projection of the field (ADR 0023), and the projection
        is ``vault_path``: the exporter writes a candidate under
        ``Agent/Promotion Candidates/`` because the row says that is where it
        lives, not because the exporter re-derives a directory. ADR 0010
        requires ``vault_path`` to stay byte-identical to the governance
        scanner's ``rel_path``, so a file in one folder and a row naming
        another would resolve its ``allowed_types`` and ``validation_mode``
        against the wrong rule. The two therefore move together or not at all,
        the same way ``set_status`` moves ``status`` and ``doc_status``.

        ``updated_at`` deliberately does not move, and here that is
        load-bearing rather than tidy: the rendered content is byte-identical
        either side of the move, which is what makes git show a rename and
        follow the file's history. Bumping the timestamp would rewrite
        ``LastUpdated`` and turn every promotion into a rename plus an edit.
        """

        statement = (
            update(vault_documents)
            .where(vault_documents.c.id == document_id)
            .values(
                promotion_status=(
                    None if promotion_status is None else promotion_status.value
                ),
                vault_path=vault_path,
            )
            .returning(*self._domain_columns)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return document_from_row(row) if row is not None else None

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

    async def list_under_path_prefixes(
        self,
        connection: AsyncConnection,
        prefixes: Sequence[str],
        after_vault_path: str | None = None,
        limit: int = 200,
    ) -> tuple[VaultDocument, ...]:
        """One ordered page of the documents living under any of ``prefixes``.

        Ordered by ``vault_path`` and paged by keyset rather than OFFSET, so a
        full walk is stable under concurrent writes and never revisits a row.
        ``vault_path`` is UNIQUE, which is what makes it a total order and a
        legal cursor.

        Unfiltered by status and by ``ai_read``, for the reason ``get_by_id``
        gives: which rows a surface may see is that surface's policy. The
        projection this serves writes files for a human, not answers for an
        agent.
        """

        if not prefixes:
            return ()

        statement = (
            select(*self._domain_columns)
            .where(
                or_(
                    *(
                        vault_documents.c.vault_path.startswith(
                            prefix, autoescape=True
                        )
                        for prefix in prefixes
                    )
                )
            )
            .order_by(vault_documents.c.vault_path)
            .limit(limit)
        )
        if after_vault_path is not None:
            statement = statement.where(
                vault_documents.c.vault_path > after_vault_path
            )
        result = await connection.execute(statement)
        return tuple(document_from_row(row) for row in result.mappings())

    async def vault_paths_under(
        self,
        connection: AsyncConnection,
        directory: str,
    ) -> set[str]:
        """Every ``vault_path`` already taken directly under ``directory``.

        Feeds ``slug.resolve_vault_path``, which needs to know what is taken
        before it can pick a free name. Returns the whole set rather than
        probing one candidate at a time: the alternative is a query per
        collision, inside the corpus-wide advisory lock, to answer a question
        one round trip already answers.

        Path-only, so this stays cheap as the corpus grows and can be served by
        the ``text_pattern_ops`` index from ADR 0010.
        """

        result = await connection.execute(
            select(vault_documents.c.vault_path).where(
                vault_documents.c.vault_path.startswith(directory, autoescape=True)
            )
        )
        return set(result.scalars())

    async def delete(
        self,
        connection: AsyncConnection,
        document_id: str,
    ) -> bool:
        """Remove a document. Returns False when no row matched.

        Embeddings go with it -- that FK cascades, because a vector for a
        document that no longer exists is not a record of anything.

        ``vault_write_requests`` does **not** cascade and must not: the ledger
        entry is what makes a replayed idempotency key a no-op, and dropping it
        would let a retired document be recreated by a retry. Its
        ``document_id`` is nullable precisely so the row can outlive its
        subject, so the pointer is cleared and the ledger keeps its meaning --
        "this key was used, and what it produced is gone".
        """

        await connection.execute(
            update(vault_write_requests)
            .where(vault_write_requests.c.document_id == document_id)
            .values(document_id=None)
        )
        # Review cases follow the same rule for the same reason: the judgement
        # is durable and the thing judged need not be. Nullable since migration
        # 0011; before it, a flagged note could never be deleted at all.
        await connection.execute(
            update(vault_review_cases)
            .where(vault_review_cases.c.candidate_document_id == document_id)
            .values(candidate_document_id=None)
        )
        result = await connection.execute(
            delete(vault_documents).where(vault_documents.c.id == document_id)
        )
        return bool(result.rowcount)

    async def count_retirement_blocking_review_references(
        self,
        connection: AsyncConnection,
        document_id: str,
    ) -> int:
        """Count review references that prevent retiring a document.

        **Only an unresolved case blocks.** ADR 0019 originally blocked on a
        candidate reference in every state, because the foreign key was durable
        and non-cascading and a decided case would have passed the service check
        only to fail at the constraint. Migration 0011 made the pointer nullable
        and ``delete`` clears it, so that mechanical reason is gone -- and with
        it the consequence, that a note flagged once could never be deleted.

        What remains is a judgement rather than a constraint: a review still in
        progress needs its subject, so retiring it out from under the reviewer
        is refused. A settled one does not, and its record survives the deletion
        with a null candidate.

        JSON evidence is unchanged and blocks on the same rule it always did --
        it names what a pending judgement was reached against, and has no
        foreign key to enforce it.
        """

        result = await connection.execute(
            select(func.count())
            .select_from(vault_review_cases)
            .where(
                vault_review_cases.c.state == ReviewState.PENDING.value,
                or_(
                    vault_review_cases.c.candidate_document_id == document_id,
                    vault_review_cases.c.similar_documents.contains(
                        [{"note_id": document_id}]
                    ),
                ),
            )
        )
        return int(result.scalar_one())


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

    async def get(
        self,
        connection: AsyncConnection,
        review_case_id: UUID,
    ) -> VaultReviewCase | None:
        result = await connection.execute(
            select(*vault_review_cases.c).where(
                vault_review_cases.c.id == review_case_id
            )
        )
        row = result.mappings().one_or_none()
        return _review_case_from_row(row) if row is not None else None

    async def list_pending(
        self,
        connection: AsyncConnection,
        limit: int = 50,
    ) -> tuple[VaultReviewCase, ...]:
        """Unresolved cases, oldest first.

        Oldest first because a review queue is a backlog rather than a feed:
        the case most at risk of being forgotten is the one that has waited
        longest. ``idx_vault_review_cases_state_created`` serves exactly this
        ordering and predicate -- it was created with the schema, before
        anything queried it.
        """

        result = await connection.execute(
            select(*vault_review_cases.c)
            .where(vault_review_cases.c.state == ReviewState.PENDING.value)
            .order_by(vault_review_cases.c.created_at, vault_review_cases.c.id)
            .limit(limit)
        )
        return tuple(_review_case_from_row(row) for row in result.mappings())

    async def decide(
        self,
        connection: AsyncConnection,
        review_case_id: UUID,
        *,
        state: ReviewState,
        decided_by: str,
        decision_note: str | None = None,
    ) -> VaultReviewCase | None:
        """Settle a pending case. Returns None when no *pending* row matched.

        The state filter is the concurrency guard, not a convenience: two
        reviewers deciding the same case at once must not both believe they
        won, because each decision also moves the document. The loser gets None
        and the caller reports a conflict.

        ``decided_by`` comes from the credential rather than the body, for the
        reason ``contributed_by`` does -- a reviewer must not be able to sign
        someone else's name to a judgement.
        """

        if state is ReviewState.PENDING:
            raise ValueError("a decision cannot leave a case pending")

        statement = (
            update(vault_review_cases)
            .where(
                vault_review_cases.c.id == review_case_id,
                vault_review_cases.c.state == ReviewState.PENDING.value,
            )
            .values(
                state=state.value,
                decided_at=func.now(),
                decided_by=decided_by,
                decision_note=decision_note,
            )
            .returning(*vault_review_cases.c)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _review_case_from_row(row) if row is not None else None


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

    async def create(
        self,
        connection: AsyncConnection,
        *,
        credential_id: str,
        principal_id: str,
        display_name: str,
        secret_sha256: bytes,
        scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> VaultCredential:
        """Insert a credential row.

        Used by the OAuth provider, which mints one per authorization and one
        per refresh rotation -- ADR 0024's "an issued access token *is* a
        credential row". ``scripts/issue_vault_credential.py`` writes its own
        insert rather than calling this: it runs against a database the
        application may not be pointed at, and an operator tool sharing the
        request path's code would make a schema change silently break the tool
        that repairs schema problems.

        The caller supplies ``secret_sha256`` rather than the secret, so the
        plaintext never crosses this boundary and cannot be logged by a query
        echo.
        """

        statement = (
            insert(vault_agent_credentials)
            .values(
                id=credential_id,
                principal_id=principal_id,
                display_name=display_name,
                secret_sha256=secret_sha256,
                scopes=sorted(set(scopes)),
                expires_at=expires_at,
            )
            .returning(*self._columns)
        )
        result = await connection.execute(statement)
        row = result.mappings().one()
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

    async def revoke(
        self,
        connection: AsyncConnection,
        credential_ids: Sequence[str],
    ) -> int:
        """Mark credentials revoked. Idempotent, and never deletes.

        ``revoked_at IS NULL`` in the predicate keeps the first revocation's
        timestamp, which is the one an incident reconstruction wants. The row
        survives so that ``last_used_at`` and the audit trail still name
        something.
        """

        if not credential_ids:
            return 0
        result = await connection.execute(
            update(vault_agent_credentials)
            .where(vault_agent_credentials.c.id.in_(list(credential_ids)))
            .where(vault_agent_credentials.c.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        return result.rowcount or 0

    async def touch(
        self,
        connection: AsyncConnection,
        credential_id: str,
    ) -> None:
        """Record a successful use, at ``TOUCH_RESOLUTION`` granularity.

        Only on success: the column means "last used", not "last attempted", so
        a failed secret must not let an attacker keep a revoked-looking
        credential looking live.

        Sampled rather than written every time. Unsampled, this turns every
        authenticated *read* into a write on one hot row per principal --
        get_note alone is quota'd at 120/min -- which is WAL churn plus row-lock
        serialization between workers on the busiest row the vault has.

        The predicate is what makes it cheap: a row already touched within the
        window does not match, so PostgreSQL updates nothing rather than
        rewriting the row with the same value. The question this column answers
        is "is this credential still in use?", asked by an operator deciding
        whether to revoke, and a minute's resolution answers it exactly as well.
        """

        await connection.execute(
            update(vault_agent_credentials)
            .where(
                vault_agent_credentials.c.id == credential_id,
                or_(
                    vault_agent_credentials.c.last_used_at.is_(None),
                    vault_agent_credentials.c.last_used_at
                    < func.now() - TOUCH_RESOLUTION,
                ),
            )
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

    async def upgrade_digest(
        self,
        connection: AsyncConnection,
        *,
        principal_id: str,
        idempotency_key: str,
        request_sha256: bytes,
        digest_version: int,
    ) -> None:
        """Restate a stored digest under the current rule.

        Called when a replay finds a digest written under a retired rule. Those
        are not recomputable -- only the digest was ever stored, never the
        payload -- so the row is uncomparable until some request restates it.
        Adopting the replaying request's digest is what makes the grandfather
        clause self-healing rather than permanent.

        Touches nothing but these two columns, so the invariant that a replay
        buys neither an embedding call nor a second document still holds.
        """

        await connection.execute(
            update(vault_write_requests)
            .where(
                vault_write_requests.c.principal_id == principal_id,
                vault_write_requests.c.idempotency_key == idempotency_key,
            )
            .values(
                request_sha256=request_sha256,
                digest_version=digest_version,
            )
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


def _registered_client_from_row(row: RowMapping) -> RegisteredOAuthClient:
    return RegisteredOAuthClient(
        client_id=row["client_id"],
        client_info=dict(row["client_info"]),
        registered_at=row["registered_at"],
        expires_at=row["expires_at"],
    )


def _pending_authorization_from_row(row: RowMapping) -> PendingAuthorization:
    return PendingAuthorization(
        client_id=row["client_id"],
        params=dict(row["params"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        csrf_sha256=row["csrf_sha256"],
    )


def _authorization_code_from_row(row: RowMapping) -> StoredAuthorizationCode:
    return StoredAuthorizationCode(
        client_id=row["client_id"],
        scopes=tuple(row["scopes"]),
        code_challenge=row["code_challenge"],
        redirect_uri=row["redirect_uri"],
        redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
        resource=row["resource"],
        subject=row["subject"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def hash_oauth_secret(secret: str) -> bytes:
    """The 32 bytes stored for a nonce or an authorization code.

    Deliberately the same construction ``auth.hash_secret`` uses for agent
    secrets, and correct for the same reason: both are machine-generated with
    full entropy, so there is no dictionary a work factor would slow down. It
    is emphatically *not* the right tool for the operator password, which is
    why ``passwords.py`` exists and uses bcrypt instead.

    A separate function rather than an import from ``auth`` because these are
    different secrets with different lifetimes; if one ever needs a different
    construction, the other should not silently follow.
    """

    return sha256(secret.encode("utf-8")).digest()


class VaultOAuthClientRepository:
    """Dynamically registered OAuth clients.

    Registration is open by decision (ADR 0024) -- the web client has no client
    id to present and the specification expects it to self-register -- which is
    why a registration grants nothing on its own. What gates access is the
    authorization an operator personally approves, and the scopes the resulting
    credential carries.
    """

    async def get(
        self,
        connection: AsyncConnection,
        client_id: str,
    ) -> RegisteredOAuthClient | None:
        result = await connection.execute(
            select(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )
        row = result.mappings().one_or_none()
        return _registered_client_from_row(row) if row is not None else None

    async def upsert(
        self,
        connection: AsyncConnection,
        *,
        client_id: str,
        client_info: Mapping[str, Any],
        expires_at: datetime | None = None,
    ) -> RegisteredOAuthClient:
        """Store a registration, replacing any earlier one for the same id.

        Upsert rather than insert because the SDK generates the client id and a
        client may legitimately re-register -- one that lost its secret repeats
        the flow, and refusing would leave it permanently unable to reconnect
        under an id it still holds. Nothing is lost: a registration carries no
        history and grants no privilege, so the current one is the only one
        that matters.
        """

        statement = (
            pg_insert(vault_oauth_clients)
            .values(
                client_id=client_id,
                client_info=dict(client_info),
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[vault_oauth_clients.c.client_id],
                set_={
                    "client_info": dict(client_info),
                    "expires_at": expires_at,
                },
            )
            .returning(*vault_oauth_clients.c)
        )
        result = await connection.execute(statement)
        return _registered_client_from_row(result.mappings().one())

    async def delete_expired(
        self,
        connection: AsyncConnection,
    ) -> int:
        """Remove registrations whose secret has expired.

        Open registration means unbounded rows, so a pruning story is required
        rather than optional (ADR 0024). Cascades to any pending authorization
        or unredeemed code for the client, which is correct: neither can
        complete once the client is gone.
        """

        result = await connection.execute(
            delete(vault_oauth_clients)
            .where(vault_oauth_clients.c.expires_at.is_not(None))
            .where(vault_oauth_clients.c.expires_at <= func.now())
        )
        return result.rowcount or 0


class VaultOAuthPendingAuthorizationRepository:
    """Authorizations in flight between ``/authorize`` and the login form.

    In Postgres and not in a dict, which is the constraint the 2026-08-22 spike
    established rather than assumed: registration arrives server-to-server from
    the vendor's backend while ``/authorize`` is a browser navigation, so the
    two halves reliably land on different Gunicorn workers. An in-memory store
    fails deterministically, and only in production.
    """

    async def create(
        self,
        connection: AsyncConnection,
        *,
        nonce: str,
        client_id: str,
        params: Mapping[str, Any],
        csrf_token: str | None = None,
        ttl_seconds: int = PENDING_AUTHORIZATION_TTL_SECONDS,
    ) -> PendingAuthorization:
        statement = (
            insert(vault_oauth_pending_authorizations)
            .values(
                nonce_sha256=hash_oauth_secret(nonce),
                client_id=client_id,
                params=dict(params),
                csrf_sha256=(
                    None if csrf_token is None else hash_oauth_secret(csrf_token)
                ),
                expires_at=func.now() + timedelta(seconds=ttl_seconds),
            )
            .returning(*vault_oauth_pending_authorizations.c)
        )
        result = await connection.execute(statement)
        return _pending_authorization_from_row(result.mappings().one())

    async def peek(
        self,
        connection: AsyncConnection,
        nonce: str,
    ) -> PendingAuthorization | None:
        """Read one without consuming it, for rendering the login form.

        The GET that shows the form must not spend the nonce -- an operator who
        reloads the page, or whose browser prefetches it, would otherwise find
        their authorization already gone. Consumption belongs to the POST, which
        is the step that mints a code. Expiry is still applied, so an expired
        nonce renders the same refusal it would on submit.
        """

        result = await connection.execute(
            select(vault_oauth_pending_authorizations)
            .where(
                vault_oauth_pending_authorizations.c.nonce_sha256
                == hash_oauth_secret(nonce)
            )
            .where(vault_oauth_pending_authorizations.c.expires_at > func.now())
        )
        row = result.mappings().one_or_none()
        return _pending_authorization_from_row(row) if row is not None else None

    async def redeem(
        self,
        connection: AsyncConnection,
        nonce: str,
    ) -> PendingAuthorization | None:
        """Consume one pending authorization, or return None.

        ``DELETE ... RETURNING`` with the expiry in the predicate, so single use
        is one atomic statement. A check-then-delete is two that a concurrent
        redemption can interleave, and this is the step that mints an
        authorization code -- letting it run twice would issue two codes for one
        approval.

        None covers an unknown nonce, an expired one, and one already redeemed,
        and the caller must not tell them apart: ADR 0024 renders one failure
        message for every outcome, because a page distinguishing "bad password"
        from "unknown request" hands an attacker a probe for valid attempts.
        """

        statement = (
            delete(vault_oauth_pending_authorizations)
            .where(
                vault_oauth_pending_authorizations.c.nonce_sha256
                == hash_oauth_secret(nonce)
            )
            .where(vault_oauth_pending_authorizations.c.expires_at > func.now())
            .returning(*vault_oauth_pending_authorizations.c)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _pending_authorization_from_row(row) if row is not None else None

    async def delete_expired(self, connection: AsyncConnection) -> int:
        result = await connection.execute(
            delete(vault_oauth_pending_authorizations).where(
                vault_oauth_pending_authorizations.c.expires_at <= func.now()
            )
        )
        return result.rowcount or 0


class VaultOAuthAuthorizationCodeRepository:
    """Minted authorization codes, between the login form and ``/token``.

    Same single-use idiom and same hashing as the pending store above, for the
    same reasons. The TTL is much shorter because redeeming a code is a
    machine-to-machine round trip that happens immediately, where a pending
    authorization waits on a person reading a consent screen.
    """

    async def create(
        self,
        connection: AsyncConnection,
        *,
        code: str,
        client_id: str,
        scopes: Sequence[str],
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        resource: str | None = None,
        subject: str | None = None,
        ttl_seconds: int = AUTHORIZATION_CODE_TTL_SECONDS,
    ) -> StoredAuthorizationCode:
        statement = (
            insert(vault_oauth_authorization_codes)
            .values(
                code_sha256=hash_oauth_secret(code),
                client_id=client_id,
                scopes=list(scopes),
                code_challenge=code_challenge,
                redirect_uri=redirect_uri,
                redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
                resource=resource,
                subject=subject,
                expires_at=func.now() + timedelta(seconds=ttl_seconds),
            )
            .returning(*vault_oauth_authorization_codes.c)
        )
        result = await connection.execute(statement)
        return _authorization_code_from_row(result.mappings().one())

    async def get(
        self,
        connection: AsyncConnection,
        code: str,
    ) -> StoredAuthorizationCode | None:
        """Look a code up without consuming it.

        The SDK's protocol splits ``load_authorization_code`` from
        ``exchange_authorization_code``, so the load must not be the redemption:
        a failed exchange after a consuming load would destroy a code the client
        is still entitled to use. Redemption is ``redeem`` below, called from
        the exchange.
        """

        result = await connection.execute(
            select(vault_oauth_authorization_codes)
            .where(
                vault_oauth_authorization_codes.c.code_sha256
                == hash_oauth_secret(code)
            )
            .where(vault_oauth_authorization_codes.c.expires_at > func.now())
        )
        row = result.mappings().one_or_none()
        return _authorization_code_from_row(row) if row is not None else None

    async def redeem(
        self,
        connection: AsyncConnection,
        code: str,
    ) -> StoredAuthorizationCode | None:
        """Consume one code, atomically, or return None.

        RFC 6749 requires an authorization code be single-use. This statement is
        what makes reuse detectable at all: the second redemption returns None
        because the first deleted the row, rather than both succeeding.
        """

        statement = (
            delete(vault_oauth_authorization_codes)
            .where(
                vault_oauth_authorization_codes.c.code_sha256
                == hash_oauth_secret(code)
            )
            .where(vault_oauth_authorization_codes.c.expires_at > func.now())
            .returning(*vault_oauth_authorization_codes.c)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _authorization_code_from_row(row) if row is not None else None

    async def delete_expired(self, connection: AsyncConnection) -> int:
        result = await connection.execute(
            delete(vault_oauth_authorization_codes).where(
                vault_oauth_authorization_codes.c.expires_at <= func.now()
            )
        )
        return result.rowcount or 0


def _refresh_token_from_row(row: RowMapping) -> StoredRefreshToken:
    return StoredRefreshToken(
        family_id=row["family_id"],
        client_id=row["client_id"],
        credential_id=row["credential_id"],
        scopes=tuple(row["scopes"]),
        subject=row["subject"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
    )


class VaultOAuthRefreshTokenRepository:
    """Refresh tokens, rotated on every use with replay detection.

    The one OAuth table that marks consumption rather than deleting, and the
    difference carries a security property rather than a preference. A deleted
    row cannot be told from a token that never existed; a consumed one can, and
    presenting a consumed refresh token is positive evidence that a token was
    captured. OAuth 2.1 requires a public client's refresh token to be
    sender-constrained or rotated with replay detection, and this is the
    detection half.
    """

    async def create(
        self,
        connection: AsyncConnection,
        *,
        token: str,
        family_id: UUID,
        client_id: str,
        credential_id: str,
        scopes: Sequence[str],
        subject: str | None = None,
        ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS,
    ) -> StoredRefreshToken:
        statement = (
            insert(vault_oauth_refresh_tokens)
            .values(
                token_sha256=hash_oauth_secret(token),
                family_id=family_id,
                client_id=client_id,
                credential_id=credential_id,
                scopes=list(scopes),
                subject=subject,
                expires_at=func.now() + timedelta(seconds=ttl_seconds),
            )
            .returning(*vault_oauth_refresh_tokens.c)
        )
        result = await connection.execute(statement)
        return _refresh_token_from_row(result.mappings().one())

    async def get(
        self,
        connection: AsyncConnection,
        token: str,
    ) -> StoredRefreshToken | None:
        """Look one up without consuming it, expired or consumed included.

        Unfiltered on purpose, unlike the authorization-code repository's
        ``get``. The SDK's ``load_refresh_token`` decides validity itself -- it
        compares ``expires_at`` and pretends an expired token does not exist --
        and the replay check needs to see a consumed row precisely because it is
        consumed. Filtering here would hide the evidence.
        """

        result = await connection.execute(
            select(vault_oauth_refresh_tokens).where(
                vault_oauth_refresh_tokens.c.token_sha256 == hash_oauth_secret(token)
            )
        )
        row = result.mappings().one_or_none()
        return _refresh_token_from_row(row) if row is not None else None

    async def consume(
        self,
        connection: AsyncConnection,
        token: str,
    ) -> StoredRefreshToken | None:
        """Mark one used, atomically, or return None if it was already used.

        ``consumed_at IS NULL`` in the predicate is what makes rotation
        single-use under concurrency: two simultaneous refreshes both match the
        digest, one wins the UPDATE, the other returns None and is treated as a
        replay. Expiry is in the predicate too, so the database clock decides
        it rather than the caller's.
        """

        statement = (
            update(vault_oauth_refresh_tokens)
            .where(
                vault_oauth_refresh_tokens.c.token_sha256 == hash_oauth_secret(token)
            )
            .where(vault_oauth_refresh_tokens.c.consumed_at.is_(None))
            .where(vault_oauth_refresh_tokens.c.expires_at > func.now())
            .values(consumed_at=func.now())
            .returning(*vault_oauth_refresh_tokens.c)
        )
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        return _refresh_token_from_row(row) if row is not None else None

    async def credential_ids_in_family(
        self,
        connection: AsyncConnection,
        family_id: UUID,
    ) -> tuple[str, ...]:
        """Every access credential ever minted in one rotation chain.

        The replay response: revoke all of them. Which one the attacker holds
        is unknown, so the answer is the whole family rather than a guess.
        """

        result = await connection.execute(
            select(vault_oauth_refresh_tokens.c.credential_id)
            .where(vault_oauth_refresh_tokens.c.family_id == family_id)
            .distinct()
        )
        return tuple(result.scalars())

    async def consume_family(
        self,
        connection: AsyncConnection,
        family_id: UUID,
    ) -> int:
        """Burn every unconsumed token in a chain, so none can be rotated again."""

        result = await connection.execute(
            update(vault_oauth_refresh_tokens)
            .where(vault_oauth_refresh_tokens.c.family_id == family_id)
            .where(vault_oauth_refresh_tokens.c.consumed_at.is_(None))
            .values(consumed_at=func.now())
        )
        return result.rowcount or 0

    async def delete_expired(self, connection: AsyncConnection) -> int:
        """Prune by expiry, never by consumption.

        A consumed token has to outlive its own rotation or replay detection
        stops working -- the whole point is recognising it when it comes back.
        Expiry is the safe boundary: past it, ``load_refresh_token`` would
        refuse the token anyway, so forgetting it costs nothing.
        """

        result = await connection.execute(
            delete(vault_oauth_refresh_tokens).where(
                vault_oauth_refresh_tokens.c.expires_at <= func.now()
            )
        )
        return result.rowcount or 0
