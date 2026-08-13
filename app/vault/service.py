"""Application-service transaction boundary for vault use cases."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from uuid import uuid4

from sqlalchemy import text as text_sql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .db import VaultPoolObserver, acquire_vault_connection
from .domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    VectorSearchStatus,
)
from .embedding_text import assemble_embedding_text, embedding_text_digest
from .embeddings import (
    EmbeddingError,
    EmbeddingInputKind,
    EmbeddingProvider,
    embed_one,
)
from .facets import normalize_facets, validate_facets
from .governance import (
    DEFAULT_POLICY,
    Action,
    ContributionOutcome,
    Flag,
    Insert,
    Link,
    Merge,
    Policy,
    Reject,
    ScoredCandidate,
    decide,
    validate,
)
from .repository import (
    VaultAuditEventRepository,
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
    VaultReviewCaseRepository,
    VaultWriteRequestRepository,
    WriteRequestRecord,
)
from .search import (
    SearchResult,
    VaultSearchRepository,
    candidate_depth,
    document_ids,
    reciprocal_rank_fusion,
)


logger = logging.getLogger(__name__)


class VaultTransactionService:
    """Own transactions while repositories remain connection-injected."""

    def __init__(
        self,
        engine: AsyncEngine,
        observer: VaultPoolObserver | None = None,
    ) -> None:
        self._engine = engine
        self._observer = observer

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with acquire_vault_connection(
            self._engine,
            self._observer,
        ) as connection:
            async with connection.begin():
                yield connection


@dataclass(frozen=True, slots=True)
class HybridSearchOutcome:
    """Fused results plus what actually contributed to them."""

    results: tuple[SearchResult, ...]
    # None when no embedding provider is configured for this process.
    profile_id: str | None
    # Why the vector arm did or did not contribute. Surfaced rather than
    # hidden: a silent quality drop is worse than a degraded answer that says
    # so, and a broken provider must not look like a deliberate one.
    vector_status: VectorSearchStatus


class VaultSearchService:
    """Read-only hybrid retrieval. Owns the transaction; repositories do not.

    ``provider`` is optional. Without one the service still answers from the
    lexical index, so a missing credential or a provider outage narrows results
    instead of removing search.
    """

    def __init__(
        self,
        transactions: VaultTransactionService,
        provider: EmbeddingProvider | None,
        text_search_config: str,
        repository: VaultSearchRepository | None = None,
    ) -> None:
        self._transactions = transactions
        self._provider = provider
        self._text_search_config = text_search_config
        self._repository = repository or VaultSearchRepository()

    async def search(self, query: str, limit: int) -> HybridSearchOutcome:
        if limit < 1:
            raise ValueError("limit must be one or greater")

        embedding, vector_status = await self._embed_query(query)
        depth = candidate_depth(limit)

        async with self._transactions.transaction() as connection:
            lexical = await self._repository.lexical_search(
                connection,
                query=query,
                text_search_config=self._text_search_config,
                limit=depth,
            )
            vector = (
                await self._repository.vector_search(
                    connection,
                    embedding=embedding,
                    profile_id=self._provider.profile_id,
                    limit=depth,
                )
                if embedding is not None and self._provider is not None
                else []
            )

            fused = reciprocal_rank_fusion(
                document_ids(lexical),
                document_ids(vector),
            )[:limit]
            documents = await self._repository.fetch_documents(
                connection,
                [hit.document_id for hit in fused],
            )

        results = tuple(
            SearchResult(
                document=documents[hit.document_id],
                score=hit.score,
                lexical_rank=hit.lexical_rank,
                vector_rank=hit.vector_rank,
            )
            for hit in fused
            # A document deleted between ranking and hydration simply drops out
            # rather than failing the whole search.
            if hit.document_id in documents
        )
        return HybridSearchOutcome(
            results=results,
            profile_id=(
                self._provider.profile_id if self._provider is not None else None
            ),
            vector_status=vector_status,
        )

    async def _embed_query(
        self,
        query: str,
    ) -> tuple[tuple[float, ...] | None, VectorSearchStatus]:
        """Embed the query, reporting why if it did not happen.

        The lexical arm needs no third party to be reachable, so a provider
        outage degrades retrieval quality instead of taking search down. The
        reason is returned rather than collapsed into None, because "nobody
        configured this" and "this is broken" need different reactions.
        """

        if self._provider is None:
            return None, VectorSearchStatus.NOT_CONFIGURED

        try:
            embedding = await embed_one(
                self._provider,
                query,
                EmbeddingInputKind.QUERY,
            )
        except EmbeddingError as exc:
            # ERROR, not WARNING: a provider was configured and did not work,
            # which is a fault rather than a deployment choice. The query text
            # is user content and the exception may quote it, so the type is
            # logged and the message is not.
            logger.error(
                "Query embedding failed; falling back to lexical search",
                extra={
                    "vault_embedding_profile_id": self._provider.profile_id,
                    "vault_embedding_error": type(exc).__name__,
                },
                exc_info=False,
            )
            return None, VectorSearchStatus.FAILED

        return embedding, VectorSearchStatus.USED


# Which rule `_canonical_request_digest` currently applies. Bump on any change
# to that function: stored digests cannot be recomputed, because the payloads
# that produced them were never kept, so the version is the only way to know
# whether a stored digest is comparable to a fresh one.
#
# 1: sha256 of model_dump_json(exclude_none=False) -- covered unset fields at
#    their defaults, so the digest moved whenever the request model gained a
#    field. Retired by migration 0006.
# 2: sha256 of model_dump_json(exclude_unset=True) -- covers only what the
#    caller sent, and is therefore stable across additive schema change.
REQUEST_DIGEST_VERSION = 2


class IdempotencyConflict(Exception):
    """Same idempotency key, different request body.

    Distinct from an ordinary failure because it maps to 409 rather than 500:
    the caller reused a key for something else, which is a client mistake to
    refuse rather than guess at.
    """


class DedupUnavailable(Exception):
    """The write path cannot run its dedup gate.

    Raised when no embedding provider is configured. The read path degrades to
    lexical-only and says so; the write path must not degrade to *no dedup*,
    because that silently defeats the one gate the vault exists to enforce. A
    refused contribution is recoverable; a corpus quietly accreting duplicates
    is not.
    """


# One corpus-wide lock, held for the whole critical section. The dangerous
# operation is check-dedup-then-write against shared index state: without it,
# two concurrent contributions can both pass dedup and both insert
# near-duplicates, and the database would happily accept them. A per-key lock
# would not help, because the conflict is between *different* keys.
#
# Serializing governed writes is acceptable at this corpus size and is exactly
# what makes the dedup decision meaningful. The constant is arbitrary but must
# never change: it is the lock's identity.
_CONTRIBUTION_LOCK_KEY = 0x5641554C5401


@dataclass(frozen=True, slots=True)
class ContributionRequest:
    """What a caller supplies. Identity and paths are assigned by the service."""

    title: str
    body: str
    contributed_by: str
    principal_id: str
    idempotency_key: str
    request_sha256: bytes
    request_id: str
    digest_version: int = REQUEST_DIGEST_VERSION
    tags: tuple[str, ...] = ()
    summary: str | None = None
    aliases: tuple[str, ...] = ()
    # {name: [values]}. Never reaches the embedding text -- see ADR 0017.
    facets: dict[str, list[str]] = field(default_factory=dict)
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None


class VaultContributionService:
    """The governed write path: validate, embed, dedup, decide, write.

    The call sequence is the ported one (vault ADR 0004), with the two changes
    the source's own migration note predicted: the decide-and-execute span runs
    inside one transaction, and embedding is hoisted to run *before* that
    transaction opens. An embedding call is a network round trip to a third
    party; holding a transaction across it would pin a pooled connection and an
    advisory lock for the provider's latency.
    """

    def __init__(
        self,
        transactions: VaultTransactionService,
        provider: EmbeddingProvider | None,
        policy: Policy = DEFAULT_POLICY,
        similar_limit: int = 5,
    ) -> None:
        self._transactions = transactions
        self._provider = provider
        self._policy = policy
        self._similar_limit = similar_limit

    async def contribute(self, request: ContributionRequest) -> ContributionOutcome:
        writes = VaultWriteRequestRepository()
        search = VaultSearchRepository()

        # 0. Idempotency, before any expensive work. A replay must not buy an
        #    embedding call, and must not become a second note that then flags
        #    as a duplicate of the first.
        async with self._transactions.transaction() as connection:
            prior = await writes.get(
                connection, request.principal_id, request.idempotency_key
            )
            if prior is not None:
                return await self._replay(connection, prior, request)

        if self._provider is None:
            raise DedupUnavailable(
                "No embedding provider is configured; refusing to write without dedup"
            )

        candidate = self._build_candidate(request)

        # Governance validation and facet vocabulary are reported together, so
        # a contribution learns everything wrong with it in one round trip
        # rather than one problem at a time.
        errors = validate(candidate) + validate_facets(candidate.facets)
        if errors:
            return ContributionOutcome(
                status="invalid",
                note_id=None,
                message="contribution failed validation",
                errors=errors,
            )

        # 1. Embed outside any transaction.
        embedding_text = assemble_embedding_text(candidate)
        vector = await embed_one(
            self._provider, embedding_text, EmbeddingInputKind.DOCUMENT
        )
        text_digest = embedding_text_digest(embedding_text)

        # 2. One transaction owns the lock, the dedup read, the decision, the
        #    writes, the idempotency record, and the audit event.
        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )

            # Re-check under the lock: another request may have settled this
            # key between step 0 and here.
            prior = await writes.get(
                connection, request.principal_id, request.idempotency_key
            )
            if prior is not None:
                return await self._replay(connection, prior, request)

            similars = await search.find_similar(
                connection,
                embedding=vector,
                profile_id=self._provider.profile_id,
                limit=self._similar_limit,
            )
            action = decide(candidate, similars, self._policy)
            return await self._execute(
                connection,
                action=action,
                request=request,
                vector=vector,
                text_digest=text_digest,
                similars=similars,
            )

    def _build_candidate(self, request: ContributionRequest) -> NewVaultDocument:
        """Assign identity, path, and governance type.

        The caller supplies content; the service owns identity. ``vault_path``
        matches what the Stage-A engine produces for an agent note, so the
        projector and the importer agree on where a contributed note lives.
        """

        document_id = uuid4().hex
        return NewVaultDocument(
            id=document_id,
            kind=DocumentKind.NOTE,
            # types.yml constrains Agent/notes/** to exactly this type.
            doc_type="Agent Note",
            vault_path=f"Agent/notes/{document_id}.md",
            status=DocumentStatus.ACTIVE,
            doc_status="Active",
            title=request.title,
            summary=request.summary,
            body=request.body,
            tags=request.tags,
            aliases=request.aliases,
            # Normalized here rather than at the transport boundary so that a
            # contribution arriving through some future non-HTTP caller is
            # stored the same way. See ADR 0017.
            facets=normalize_facets(request.facets),
            related_ids=request.related_ids,
            source_ids=request.source_ids,
            contributed_by=request.contributed_by,
            source_url=request.source_url,
            provenance={"principal_id": request.principal_id},
        )

    @staticmethod
    async def _replay(
        connection: AsyncConnection,
        prior: WriteRequestRecord,
        request: ContributionRequest,
    ) -> ContributionOutcome:
        # A digest is only evidence about the body when both sides were computed
        # the same way. Rows written under an older rule are not recomputable --
        # only the digest was stored, never the payload -- so a mismatch there is
        # an absence of evidence, not evidence of a different request. Refusing
        # on it would turn every pre-0006 key into a permanent 409 that no
        # caller could clear, which is exactly what stranded the 48 imported
        # notes.
        #
        # Restate the digest under the current rule while replaying, so the row
        # becomes comparable again and the concession lasts one call instead of
        # forever. Two concurrent replays of the same key write the same value,
        # and two *different* bodies racing here resolve to whichever lands last
        # -- the same "first request after the migration wins" property the
        # grandfather clause already has, not a new one.
        if prior.digest_version != REQUEST_DIGEST_VERSION:
            logger.info(
                "vault write request replayed without digest verification",
                extra={
                    "vault_principal_id": prior.principal_id,
                    "vault_idempotency_key": prior.idempotency_key,
                    "vault_stored_digest_version": prior.digest_version,
                    "vault_current_digest_version": REQUEST_DIGEST_VERSION,
                },
            )
            await VaultWriteRequestRepository().upgrade_digest(
                connection,
                principal_id=prior.principal_id,
                idempotency_key=prior.idempotency_key,
                request_sha256=request.request_sha256,
                digest_version=REQUEST_DIGEST_VERSION,
            )
        elif prior.request_sha256 != request.request_sha256:
            raise IdempotencyConflict(
                "idempotency key was already used for a different request body"
            )
        stored = prior.response or {}
        return ContributionOutcome(
            status=stored.get("status", prior.state),
            note_id=prior.document_id,
            message=stored.get("message", "idempotent replay"),
            idempotent_replay=True,
        )

    async def _settle(
        self,
        connection: AsyncConnection,
        *,
        outcome: ContributionOutcome,
        state: str,
        request: ContributionRequest,
    ) -> ContributionOutcome:
        """Record the idempotency result and the audit event, in-transaction.

        Both share the document's transaction so a replay can never observe a
        document without its idempotency record, or the reverse.
        """

        # The top similarity this contribution scored against the corpus, kept
        # so `flag_at` calibration accumulates from real traffic instead of only
        # from re-running scripts/measure_dedup_similarity.py. Every settled
        # write is one more observation of where legitimate contributions sit,
        # which is the floor half of the two-sided derivation in
        # docs/embedding-calibration.md.
        #
        # A score only, never an id or a title: this column is read back on
        # replay, and naming a document the contributor may not read would
        # reopen the disclosure channel find_similar closes.
        #
        # Write requests are prunable, so this is a rolling window rather than a
        # durable series — harvest into the model register before pruning.
        top_similarity = (
            max(candidate.score for candidate in outcome.similars)
            if outcome.similars
            else None
        )

        await VaultWriteRequestRepository().complete(
            connection,
            principal_id=request.principal_id,
            idempotency_key=request.idempotency_key,
            request_sha256=request.request_sha256,
            digest_version=request.digest_version,
            state=state,
            document_id=outcome.note_id,
            response={
                "status": outcome.status,
                "message": outcome.message,
                "top_similarity": top_similarity,
            },
        )
        await VaultAuditEventRepository().record(
            connection,
            operation="vault.contribute",
            outcome=outcome.status,
            request_id=request.request_id,
            principal_id=request.principal_id,
            target_type="document" if outcome.note_id else None,
            target_id=outcome.note_id,
            idempotency_key=request.idempotency_key,
        )
        return outcome

    async def _store(
        self,
        connection: AsyncConnection,
        note: NewVaultDocument,
        vector: tuple[float, ...],
        text_digest: bytes,
    ) -> str:
        stored = await VaultDocumentRepository().insert(connection, note)
        await VaultDocumentEmbeddingRepository().upsert(
            connection,
            DocumentEmbedding(
                document_id=stored.id,
                profile_id=self._provider.profile_id,
                vector=vector,
                text_sha256=text_digest,
            ),
        )
        return stored.id

    async def _execute(
        self,
        connection: AsyncConnection,
        *,
        action: Action,
        request: ContributionRequest,
        vector: tuple[float, ...],
        text_digest: bytes,
        similars: list[ScoredCandidate],
    ) -> ContributionOutcome:
        match action:
            case Insert(note=note):
                note_id = await self._store(connection, note, vector, text_digest)
                return await self._settle(
                    connection,
                    outcome=ContributionOutcome(
                        status="inserted",
                        note_id=note_id,
                        message="note added to vault",
                        similars=similars,
                    ),
                    state="inserted",
                    request=request,
                )

            case Flag(note=note, reason=reason, similars=sims):
                # Written as flagged rather than withheld: the review queue
                # needs the content to adjudicate it, and ADR 0008 already
                # keeps the read surface from serving a flagged document.
                flagged = replace(
                    note, status=DocumentStatus.FLAGGED, doc_status="Flagged"
                )
                note_id = await self._store(
                    connection, flagged, vector, text_digest
                )
                await VaultReviewCaseRepository().insert_pending(
                    connection,
                    candidate_document_id=note_id,
                    reason=reason,
                    similar_documents=[
                        {"note_id": s.note_id, "title": s.title, "score": s.score}
                        for s in sims
                    ],
                )
                return await self._settle(
                    connection,
                    outcome=ContributionOutcome(
                        status="flagged",
                        note_id=note_id,
                        message=f"flagged for review: {reason}",
                        similars=sims,
                    ),
                    state="flagged",
                    request=request,
                )

            case Reject(reason=reason, conflicting_id=conflicting):
                return await self._settle(
                    connection,
                    outcome=ContributionOutcome(
                        status="rejected",
                        note_id=None,
                        message=f"rejected: {reason} (conflicts with {conflicting})",
                        similars=similars,
                    ),
                    # No dedicated enum value. reject_at is disabled under the
                    # current policy, so this is unreachable today; "invalid"
                    # is the closest settled state if a policy enables it.
                    state="invalid",
                    request=request,
                )

            case Link():
                raise NotImplementedError(
                    "Link requires link_at, disabled under the current policy"
                )

            case Merge():
                # Vault ADR 0004: automatic merge stays disabled. Reaching here
                # means a policy set merge_at, which is a decision nobody made.
                raise NotImplementedError(
                    "Merge requires a real merge strategy; deferred by ADR 0004"
                )
