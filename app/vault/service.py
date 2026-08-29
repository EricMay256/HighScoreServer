"""Application-service transaction boundary for vault use cases."""

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text as text_sql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .body_diff import (
    BodyChangeSummary,
    BodyDiffError,
    apply_body_unified_diff,
    span_edit_to_unified_diff,
    summarize_body_change,
)
from .constants import (
    CORPUS_LOCK_KEY,
    NOTE_SCHEMA_VERSION,
    SUMMARY_GRACE_PERIOD_SECONDS,
    WIKI_SCHEMA_VERSION,
)
from .db import VaultPoolObserver, acquire_vault_connection
from .domain import (
    AmendmentProposalKind,
    AmendmentProposalState,
    CompileRunState,
    CompileWorkItem,
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    NoteCompileState,
    PromotionStatus,
    ReviewState,
    VaultAmendmentProposal,
    VaultCompileRun,
    VaultDocument,
    VaultReviewCase,
    VectorSearchStatus,
)
from .embedding_text import assemble_embedding_text, embedding_text_digest
from .embeddings import (
    EmbeddingError,
    EmbeddingInputKind,
    EmbeddingProvider,
    embed_one,
)
from .facets import FacetNameCollision, normalize_facets, validate_facets
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
from .origin import normalize_origin, validate_origin
from .read_policy import READABLE_STATUSES
from .repository import (
    VaultAmendmentProposalRepository,
    VaultAuditEventRepository,
    VaultCompileRunRepository,
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
    VaultReviewCaseRepository,
    VaultWikiPageRepository,
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
from .slug import resolve_vault_path


logger = logging.getLogger(__name__)


# Where a contributed agent note lives. Supplied by the service and never
# derived from caller input -- that separation is the whole of ADR 0022's
# privilege argument, and types.yml constrains this folder to "Agent Note".
AGENT_NOTES_DIRECTORY = "Agent/notes/"

# Where compilation will put a wiki page. Named here rather than in export.py
# so the promotion verb can send a retracted page back to the folder it came
# from: folders.yml types that one "Wiki Page" and `Agent/notes/` "Agent Note",
# so returning a page to the notes folder would leave it violating the rule its
# own path selects. Nothing writes it yet -- compilation is NEXT-STEPS item 5.
AGENT_WIKI_DIRECTORY = "Agent/wiki/"

# Where a promotion candidate is projected. Both kinds land here: ADR 0023
# widened `allowed_types` to ["Agent Note", "Wiki Page"] on the reasoning that a
# compiled page distilling several notes is, if anything, more human-worthy than
# a raw note.
PROMOTION_CANDIDATES_DIRECTORY = "Agent/Promotion Candidates/"


def _promotion_home_directory(kind: DocumentKind) -> str:
    """Where a document lives when it is not a promotion candidate.

    Keyed on `kind` rather than on the current path, so the answer is a
    property of the document rather than of where it happens to be sitting --
    which is what a candidate's path deliberately is not.
    """

    return (
        AGENT_WIKI_DIRECTORY
        if kind is DocumentKind.WIKI
        else AGENT_NOTES_DIRECTORY
    )


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
    async def transaction(
        self,
        isolation_level: str | None = None,
    ) -> AsyncIterator[AsyncConnection]:
        """A connection with a transaction open on it.

        ``isolation_level`` is None for almost everything, meaning the server
        default -- READ COMMITTED. Pass ``"REPEATABLE READ"`` when a *multi
        statement read* has to see one corpus: under READ COMMITTED every
        statement takes a fresh snapshot, so sharing a transaction buys
        atomicity for writes but no consistency at all for a paged walk. Set
        before ``begin()`` because Postgres will not accept it once the
        transaction has started.
        """

        async with acquire_vault_connection(
            self._engine,
            self._observer,
        ) as connection:
            if isolation_level is not None:
                await connection.execution_options(
                    isolation_level=isolation_level
                )
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
    # Whether fusion ranked anything below the page that was returned. Free to
    # compute -- fusion already produces the whole ranking and the page is a
    # slice of it -- so this is an observation rather than an extra query.
    #
    # Bounded by the candidate window, and it has to be: each arm fetches
    # `candidate_depth(limit)` rows, capped at MAX_CANDIDATES. So it means
    # "fusion ranked more than fitted on this page", not "the corpus contains
    # more matches". For deciding whether to narrow a query those are the same
    # answer, which is what it is for.
    has_more: bool = False


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

            ranked = reciprocal_rank_fusion(
                document_ids(lexical),
                document_ids(vector),
            )
            # Slice after fusion rather than before it, so the count below is
            # the real remainder. Hydration is still one page wide -- the
            # bodies are what a search costs, and nothing off the page is
            # fetched.
            fused = ranked[:limit]
            has_more = len(ranked) > limit
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
            has_more=has_more,
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
#    caller sent, and is therefore stable across additive schema change, but
#    nested object keys retained caller insertion order.
# 3: sha256 of compact JSON over the JSON-mode model dump, recursively sorting
#    object keys while preserving list order.
REQUEST_DIGEST_VERSION = 3


class IdempotencyConflict(Exception):
    """Same idempotency key, different request body.

    Distinct from an ordinary failure because it maps to 409 rather than 500:
    the caller reused a key for something else, which is a client mistake to
    refuse rather than guess at.
    """

    def __init__(self, message: str, document_id: str | None) -> None:
        super().__init__(message)
        self.document_id = document_id


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
# Defined in `constants` now that an operator script takes it too -- the
# backfill's writes have to serialize with these. Same reasoning as
# OAUTH_CLIENT_LOCK_KEY: a lock two modules disagree about is not a lock.
_CONTRIBUTION_LOCK_KEY = CORPUS_LOCK_KEY


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
    # Upstream provenance for content authored before it reached the vault.
    # Empty for an ordinary contribution. See origin.py.
    origin: dict[str, str] = field(default_factory=dict)
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None


async def _summary_still_repairable(
connection: AsyncConnection,
    *,
    document_id: str | None,
    contributed_by: str,
) -> bool:
    """Whether ``vault_set_summary`` would succeed on this note right now.

    The same conditions its predicate applies, asked ahead of time so a
    replay can decide whether to repeat the advice. Read-only and best
    effort: a false positive costs one refused follow-up call, where the
    false negative it replaces cost a lost-response caller any knowledge
    that its note has no summary at all.
    """

    if document_id is None:
        return False
    note = await VaultDocumentRepository().get_by_id(
        connection,
        document_id,
        statuses=READABLE_STATUSES,
        readable_only=True,
    )
    if note is None or note.kind is not DocumentKind.NOTE:
        return False
    if note.summary is not None or note.contributed_by != contributed_by:
        return False
    not_before = datetime.now(UTC) - timedelta(
        seconds=SUMMARY_GRACE_PERIOD_SECONDS
    )
    return note.created_at >= not_before


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
        """Run a contribution and durably audit any idempotency conflict."""

        try:
            return await self._contribute(request)
        except IdempotencyConflict as exc:
            # The transaction that detects the mismatch rolls back when the
            # exception leaves it. Record the refused attempt separately so
            # raising the route's 409 cannot roll back its audit evidence.
            async with self._transactions.transaction() as connection:
                await VaultAuditEventRepository().record(
                    connection,
                    operation="vault.contribute",
                    outcome="conflict",
                    request_id=request.request_id,
                    principal_id=request.principal_id,
                    target_type="document" if exc.document_id else None,
                    target_id=exc.document_id,
                    idempotency_key=request.idempotency_key,
                )
            raise

    async def _contribute(self, request: ContributionRequest) -> ContributionOutcome:
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

        try:
            candidate = self._build_candidate(request)
        except FacetNameCollision as exc:
            return ContributionOutcome(
                status="invalid",
                note_id=None,
                message="contribution failed validation",
                errors=(str(exc),),
            )

        # Governance validation and facet vocabulary are reported together, so
        # a contribution learns everything wrong with it in one round trip
        # rather than one problem at a time.
        errors = (
            validate(candidate)
            + validate_facets(candidate.facets)
            + validate_origin(candidate.origin)
        )
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
            # A second query over the corpus the first deliberately excludes.
            # It reaches the response and nothing else: not `decide()`, not
            # `top_similarity`, not the calibration register. A compiled page
            # restates its sources, so resembling one is expected rather than
            # evidence (ADR 0027).
            related_pages = await search.find_related_pages(
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
                related_pages=related_pages,
            )

    def _build_candidate(self, request: ContributionRequest) -> NewVaultDocument:
        """Assign identity, path, and governance type.

        The caller supplies content; the service owns identity. ``vault_path``
        matches what the Stage-A engine produces for an agent note, so the
        projector and the importer agree on where a contributed note lives --
        including the title slug, so the exported tree is browsable by a human
        rather than a folder of hex (ADR 0022's 2026-08-20 amendment).

        The path assigned here is the *uncontended* one. Collision suffixing
        needs to know what is already taken, which is a query whose answer only
        stays true under the corpus lock, so ``_resolve_path`` settles it later.
        Nothing between here and there reads ``vault_path``: governance
        validation checks content, and the embedding text is title, aliases,
        tags, summary, and body (ADR 0013).
        """

        document_id = uuid4().hex
        return NewVaultDocument(
            id=document_id,
            kind=DocumentKind.NOTE,
            # types.yml constrains Agent/notes/** to exactly this type.
            doc_type="Agent Note",
            # The governance document schema this note is written under.
            # Assigned here rather than left to NewVaultDocument's default,
            # which said 1 while every note in the corpus said 2.
            schema_version=NOTE_SCHEMA_VERSION,
            vault_path=resolve_vault_path(AGENT_NOTES_DIRECTORY, request.title, ()),
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
            # Normalized here rather than at the transport boundary, for the
            # same reason facets are: a contribution arriving through a future
            # non-HTTP caller must be stored the same way.
            origin=normalize_origin(request.origin),
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
                "idempotency key was already used for a different request body",
                document_id=prior.document_id,
            )
        stored = prior.response or {}
        await VaultAuditEventRepository().record(
            connection,
            operation="vault.contribute",
            outcome="replayed",
            request_id=request.request_id,
            principal_id=request.principal_id,
            target_type="document" if prior.document_id else None,
            target_id=prior.document_id,
            idempotency_key=request.idempotency_key,
        )
        return ContributionOutcome(
            status=stored.get("status", prior.state),
            note_id=prior.document_id,
            message=stored.get("message", "idempotent replay"),
            idempotent_replay=True,
            summary_repairable=await _summary_still_repairable(
                connection,
                document_id=prior.document_id,
                contributed_by=request.contributed_by,
            ),
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

    @staticmethod
    async def _resolve_path(
        connection: AsyncConnection,
        note: NewVaultDocument,
    ) -> NewVaultDocument:
        """Settle the note's slug against the paths already taken.

        Called under the corpus-wide advisory lock, which is the only place the
        answer stays true between reading it and inserting on it. Two notes may
        legitimately carry one title -- the dedup gate scores meaning, not
        titles -- and ``vault_path`` is UNIQUE, so an unsuffixed second one is
        an IntegrityError rather than a rare event.

        The directory is the service's, never the caller's: ``slugify``
        collapses every non-alphanumeric run to a hyphen, so no separator
        survives a title into the path.
        """

        taken = await VaultDocumentRepository().vault_paths_under(
            connection, AGENT_NOTES_DIRECTORY
        )
        resolved = resolve_vault_path(AGENT_NOTES_DIRECTORY, note.title, taken)
        return note if resolved == note.vault_path else replace(
            note, vault_path=resolved
        )

    async def _store(
        self,
        connection: AsyncConnection,
        note: NewVaultDocument,
        vector: tuple[float, ...],
        text_digest: bytes,
    ) -> str:
        note = await self._resolve_path(connection, note)
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
        related_pages: list[ScoredCandidate] | None = None,
    ) -> ContributionOutcome:
        pages = list(related_pages or [])
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
                        related_pages=pages,
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
                        related_pages=pages,
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
                        related_pages=pages,
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


class DocumentNotFound(Exception):
    """No document with that id is visible to this caller.

    Distinct from a generic failure because it maps to 404. Deliberately does
    not distinguish "no such row" from "exists but you may not read it": ADR
    0014 keeps the read surface from confirming that a document exists in a
    folder the caller cannot see, and an update surface that leaked it would
    reopen the channel the dedup query already closes.
    """


class UpdateWouldDuplicate(Exception):
    """The replacement content collides with a *different* document.

    Carries the matches so the caller can see what it collided with.
    """

    def __init__(self, message: str, similars: Sequence[ScoredCandidate]) -> None:
        super().__init__(message)
        self.similars = tuple(similars)


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    """A full replacement of one document's caller-supplied content."""

    document_id: str
    title: str
    body: str
    principal_id: str
    request_id: str
    summary: str | None = None
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    facets: dict[str, list[str]] = field(default_factory=dict)
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateOutcome:
    """What a settled replacement did."""

    note_id: str
    message: str
    re_embedded: bool
    errors: tuple[str, ...] = ()


class VaultDocumentUpdateService:
    """Replace an existing document's content, through the same gates.

    Shares the contribution path's shape deliberately -- validate, embed outside
    the transaction, take the corpus lock, run dedup, write -- because an update
    can create a duplicate just as a contribution can, and a surface that
    skipped the gate would be the easy way around it.

    Three things differ, each for a stated reason:

    - **Dedup excludes the document being updated.** It would otherwise score
      1.0 against itself and every edit would look like a duplicate.
    - **A collision refuses instead of flagging.** A contribution that flags is
      still written, because the review queue needs the content to adjudicate.
      An update that flagged would take an existing, active, readable document
      out of the read surface as a side effect of an edit -- a strictly worse
      state than before the call, reached by a caller trying to improve it.
      Refusing leaves the document exactly as it was and says what it hit.
    - **Embedding is conditional.** ``embedded_text_sha256`` already answers
      "did the text that produced this vector change" (ADR 0013), so an edit
      touching only facets or related_ids costs no embedding call.
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

    async def update(self, request: UpdateRequest) -> UpdateOutcome:
        documents = VaultDocumentRepository()
        embeddings = VaultDocumentEmbeddingRepository()
        search = VaultSearchRepository()

        if self._provider is None:
            raise DedupUnavailable(
                "No embedding provider is configured; refusing to write without dedup"
            )

        # Load first, outside the lock: the replacement is validated against the
        # row's own governance type and path, and an embedding call for a
        # document that does not exist is wasted.
        async with self._transactions.transaction() as connection:
            existing = await documents.get_by_id(
                connection,
                request.document_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )
            stored = (
                None
                if existing is None
                else await embeddings.get(
                    connection, request.document_id, self._provider.profile_id
                )
            )
        if existing is None:
            raise DocumentNotFound(request.document_id)

        try:
            candidate = self._build_candidate(existing, request)
        except FacetNameCollision as exc:
            return UpdateOutcome(
                note_id=request.document_id,
                message="update failed validation",
                re_embedded=False,
                errors=(str(exc),),
            )

        errors = validate(candidate) + validate_facets(candidate.facets)
        if errors:
            return UpdateOutcome(
                note_id=request.document_id,
                message="update failed validation",
                re_embedded=False,
                errors=tuple(errors),
            )

        # Embed only when the text the vector was built from actually moved.
        embedding_text = assemble_embedding_text(candidate)
        text_digest = embedding_text_digest(embedding_text)
        re_embed = stored is None or stored.text_sha256 != text_digest
        vector = (
            await embed_one(self._provider, embedding_text, EmbeddingInputKind.DOCUMENT)
            if re_embed
            else stored.vector
        )

        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )

            similars = await search.find_similar(
                connection,
                embedding=vector,
                profile_id=self._provider.profile_id,
                limit=self._similar_limit,
                exclude_document_id=request.document_id,
            )
            match decide(candidate, similars, self._policy):
                case Insert():
                    pass
                case Flag(reason=reason, similars=sims):
                    raise UpdateWouldDuplicate(f"update refused: {reason}", sims)
                case Reject(reason=reason, conflicting_id=conflicting):
                    raise UpdateWouldDuplicate(
                        f"update refused: {reason} (conflicts with {conflicting})",
                        similars,
                    )
                case Link():
                    raise NotImplementedError(
                        "Link requires link_at, disabled under the current policy"
                    )
                case Merge():
                    raise NotImplementedError(
                        "Merge requires a real merge strategy; deferred by ADR 0004"
                    )

            updated = await documents.replace_content(
                connection, request.document_id, candidate
            )
            if updated is None:
                # Visible a moment ago and not now. Nothing deletes documents
                # today, so this is a race rather than an ordinary miss --
                # surface it as the 404 it is.
                raise DocumentNotFound(request.document_id)

            # Always persist the candidate vector, even when producing it did
            # not require a provider call. ``re_embed`` was decided from a
            # pre-lock snapshot; another update may have changed both text and
            # vector while this request waited. Re-applying the candidate's
            # already-known vector keeps the final row and embedding atomic.
            await embeddings.upsert(
                connection,
                DocumentEmbedding(
                    document_id=request.document_id,
                    profile_id=self._provider.profile_id,
                    vector=vector,
                    text_sha256=text_digest,
                ),
            )

            await VaultAuditEventRepository().record(
                connection,
                operation="vault.update",
                outcome="updated",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=request.document_id,
            )

        return UpdateOutcome(
            note_id=request.document_id,
            message=(
                "document replaced and re-embedded"
                if re_embed
                else "document replaced; embedding text unchanged"
            ),
            re_embedded=re_embed,
        )

    @staticmethod
    def _build_candidate(
        existing: VaultDocument, request: UpdateRequest
    ) -> NewVaultDocument:
        """The row as it would be after the replacement.

        Identity, path, kind, governance type, status and contributor come from
        the existing row; only what a caller may supply comes from the request.
        Built as a ``NewVaultDocument`` so it satisfies the same ``validate``
        and ``assemble_embedding_text`` the contribution path uses -- the point
        of running the same gate is running exactly the same gate.
        """

        return NewVaultDocument(
            id=existing.id,
            kind=existing.kind,
            doc_type=existing.doc_type,
            vault_path=existing.vault_path,
            status=existing.status,
            doc_status=existing.doc_status,
            title=request.title,
            summary=request.summary,
            body=request.body,
            tags=request.tags,
            aliases=request.aliases,
            facets=normalize_facets(request.facets),
            related_ids=request.related_ids,
            source_ids=request.source_ids,
            contributed_by=existing.contributed_by,
            source_url=request.source_url,
            provenance=existing.provenance,
        )



class SummaryAlreadyPresent(Exception):
    """The note already carries a summary, so the carveout does not apply.

    Distinct from a refusal to authorize: the caller may well be the author,
    inside the window, and simply asking for something this operation cannot
    do. ADR 0035 makes the carveout monotonic on purpose, and rewriting an
    existing precis is what ``vault:update`` and the proposal workflow are for.
    """


class SummaryWindowClosed(Exception):
    """The note is the caller's own, but was contributed too long ago.

    Carries the window so the caller is told the rule rather than left to infer
    it from a bare refusal.
    """

    def __init__(self, message: str, grace_seconds: int) -> None:
        super().__init__(message)
        self.grace_seconds = grace_seconds


class SummaryRejected(Exception):
    """The note would fail governance validation with this summary."""


class SummaryStale(Exception):
    """The note's content changed while this summary was being embedded.

    Retryable, and the distinction from ``SummaryAlreadyPresent`` matters: that
    one is settled and a retry would be wrong, this one succeeds on a second
    attempt against the new revision. It exists because the embedding call
    happens outside the corpus lock, so a vector computed from the snapshot can
    arrive describing content that has since moved.
    """


@dataclass(frozen=True, slots=True)
class SetSummaryRequest:
    """Supply the ``summary`` a contribution omitted.

    ``contributed_by`` is the caller's stored identity rather than a free
    field: each adapter derives it from the credential exactly as the
    contribution path does, so it is never caller-supplied at either one.
    Passing it in rather than deriving it here keeps the service
    transport-neutral, which is the same reason ``principal_id`` arrives this
    way.
    """

    document_id: str
    summary: str
    principal_id: str
    contributed_by: str
    request_id: str


@dataclass(frozen=True, slots=True)
class SetSummaryOutcome:
    """What a settled carveout write did."""

    note_id: str
    message: str
    content_revision: int


class VaultDocumentSummaryService:
    """Fill in an absent ``summary`` on the caller's own recent note (ADR 0035).

    This exists because of a measured gap and a scope boundary that made the
    gap self-perpetuating. ``summary`` joins the embedding text and is the
    search preview, so an absent one costs retrieval quality -- and yet the
    agent that omitted it could not fix it, because the general update path is
    ``vault:update``, which ADR 0024 deliberately keeps off the OAuth baseline.
    Its only recourse was an amendment proposal: a full-replacement,
    revision-bound workflow record needing a second credential to adjudicate,
    in order to add one field to a note it wrote thirty seconds earlier.

    So the carveout is narrow enough to sit under ``vault:write``, and narrow
    in three independent ways rather than one:

    - **One field.** Not a replacement wearing a smaller name. The body, the
      title and the tags are unreachable here, which is what stops a note being
      contributed innocuously and then rewritten once it is in the corpus.
    - **Only where the field is empty.** The operation is monotonic, so it can
      never misrepresent a note that already describes itself.
    - **Only inside the grace period, on the caller's own note.** The window is
      the part doing security work: an embedded, previewed field is a
      retrieval-poisoning vector, and bounding it in time means the principal
      amending the note is the one that just wrote it, rather than one that has
      since read hostile instructions out of the corpus.

    **The dedup gate still runs.** Changing the summary changes the embedding
    text, and ``VaultDocumentUpdateService`` is explicit that a surface
    skipping the gate would be the easy way around it. A carveout is not an
    exemption from the vault's one invariant; it is a narrower door through the
    same wall. Like an update and unlike a contribution, a collision *refuses*
    rather than flagging: the note is already active and readable, and taking
    it out of the read surface as a side effect of describing it would leave
    the caller worse off than if it had never called.
    """

    def __init__(
        self,
        transactions: VaultTransactionService,
        provider: EmbeddingProvider | None,
        policy: Policy = DEFAULT_POLICY,
        similar_limit: int = 5,
        grace_seconds: int = SUMMARY_GRACE_PERIOD_SECONDS,
    ) -> None:
        self._transactions = transactions
        self._provider = provider
        self._policy = policy
        self._similar_limit = similar_limit
        self._grace_seconds = grace_seconds

    async def set_summary(self, request: SetSummaryRequest) -> SetSummaryOutcome:
        documents = VaultDocumentRepository()
        embeddings = VaultDocumentEmbeddingRepository()
        search = VaultSearchRepository()

        if self._provider is None:
            raise DedupUnavailable(
                "No embedding provider is configured; refusing to write without dedup"
            )

        # One clock for the whole call, computed before the read and reused for
        # the conditional write. Reading the time again at the write would let
        # a request that passed the window check fail the identical check a few
        # milliseconds later, which is a race nobody can act on.
        not_before = datetime.now(UTC) - timedelta(seconds=self._grace_seconds)

        # Load outside the lock, as the contribution and update paths do: an
        # embedding call for a note this caller may not touch is wasted, and
        # the refusals below are all decidable from the row.
        async with self._transactions.transaction() as connection:
            existing = await documents.get_by_id(
                connection,
                request.document_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )

        # Three different misses collapse into one 404, deliberately. "No such
        # note", "not yours" and "not a note" all mean the same thing to this
        # operation -- it does not address that document -- and separating them
        # would let a caller enumerate authorship across the corpus, which is
        # the disclosure ADR 0014 closes on the read surface and ADR 0016
        # closes on the dedup query.
        if (
            existing is None
            or existing.kind is not DocumentKind.NOTE
            or existing.contributed_by != request.contributed_by
        ):
            raise DocumentNotFound(request.document_id)

        # These two disclose nothing new: the caller has just proved it wrote
        # this note, so it is owed the reason.
        if existing.summary is not None:
            raise SummaryAlreadyPresent(
                f"{request.document_id} already has a summary; this operation "
                "only fills in an absent one"
            )
        if existing.created_at < not_before:
            raise SummaryWindowClosed(
                f"{request.document_id} was contributed more than "
                f"{self._grace_seconds // 60} minutes ago; propose an "
                "amendment instead",
                self._grace_seconds,
            )

        candidate = self._build_candidate(existing, request.summary)

        # ``validate`` only, not ``validate_facets``. The facets are the stored
        # ones and this caller cannot change them, so failing here on data it
        # has no way to fix would refuse a legitimate write for someone else's
        # mistake. The document-level gate still runs, because the row is about
        # to carry content the caller supplied.
        errors = validate(candidate)
        if errors:
            raise SummaryRejected("; ".join(errors))

        # Unconditional, unlike the update path's. The summary moved from
        # absent to present, so the embedding text moved by construction and
        # the ``embedded_text_sha256`` comparison could only ever agree.
        embedding_text = assemble_embedding_text(candidate)
        text_digest = embedding_text_digest(embedding_text)
        vector = await embed_one(
            self._provider, embedding_text, EmbeddingInputKind.DOCUMENT
        )

        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )

            similars = await search.find_similar(
                connection,
                embedding=vector,
                profile_id=self._provider.profile_id,
                limit=self._similar_limit,
                exclude_document_id=request.document_id,
            )
            match decide(candidate, similars, self._policy):
                case Insert():
                    pass
                case Flag(reason=reason, similars=sims):
                    raise UpdateWouldDuplicate(f"summary refused: {reason}", sims)
                case Reject(reason=reason, conflicting_id=conflicting):
                    raise UpdateWouldDuplicate(
                        f"summary refused: {reason} (conflicts with {conflicting})",
                        similars,
                    )
                case Link():
                    raise NotImplementedError(
                        "Link requires link_at, disabled under the current policy"
                    )
                case Merge():
                    raise NotImplementedError(
                        "Merge requires a real merge strategy; deferred by ADR 0004"
                    )

            updated = await documents.set_summary(
                connection,
                request.document_id,
                summary=request.summary,
                contributed_by=request.contributed_by,
                not_before=not_before,
                expected_revision=existing.content_revision,
            )
            if updated is None:
                # The predicate is the authority and it has just disagreed with
                # a check that passed moments ago. Re-read under the lock to say
                # which condition moved, because they need different answers: a
                # summary that arrived is settled, and content that changed is
                # retryable. Guessing "already present" was wrong -- ordinary
                # updates and accepted amendments take this same lock, so any of
                # them could have been the writer in between.
                await self._refuse_stale_summary(
                    connection,
                    documents,
                    request=request,
                    not_before=not_before,
                    expected_revision=existing.content_revision,
                )

            await embeddings.upsert(
                connection,
                DocumentEmbedding(
                    document_id=request.document_id,
                    profile_id=self._provider.profile_id,
                    vector=vector,
                    text_sha256=text_digest,
                ),
            )

            await VaultAuditEventRepository().record(
                connection,
                operation="vault.set_summary",
                outcome="summary_set",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=request.document_id,
            )

        return SetSummaryOutcome(
            note_id=request.document_id,
            message="summary set and note re-embedded",
            content_revision=updated.content_revision,
        )

    async def _refuse_stale_summary(
        self,
        connection: AsyncConnection,
        documents: VaultDocumentRepository,
        *,
        request: SetSummaryRequest,
        not_before: datetime,
        expected_revision: int,
    ) -> None:
        """Say which precondition moved, having re-read the row under the lock.

        Always raises. The order matters: the conditions are checked from the
        most settled to the most retryable, so a note that both gained a summary
        and moved on reports the summary -- retrying that would be pointless
        where retrying a revision change is not.
        """

        current = await documents.get_by_id(
            connection,
            request.document_id,
            statuses=READABLE_STATUSES,
            readable_only=True,
        )
        if (
            current is None
            or current.kind is not DocumentKind.NOTE
            or current.contributed_by != request.contributed_by
        ):
            raise DocumentNotFound(request.document_id)
        if current.summary is not None:
            raise SummaryAlreadyPresent(
                f"{request.document_id} gained a summary while this "
                "request was in flight"
            )
        if current.created_at < not_before:
            raise SummaryWindowClosed(
                f"{request.document_id} was contributed more than "
                f"{self._grace_seconds // 60} minutes ago; propose an "
                "amendment instead",
                self._grace_seconds,
            )
        if current.content_revision != expected_revision:
            raise SummaryStale(
                f"{request.document_id} changed while this summary was being "
                f"embedded (revision {expected_revision} -> "
                f"{current.content_revision}); retry against the current text"
            )
        # No condition explains the miss, which means the predicate and this
        # re-read disagree about the same row -- report it rather than invent a
        # reason, and do not write the vector either way.
        raise SummaryStale(
            f"{request.document_id} did not accept the summary and no "
            "precondition explains it; retry"
        )

    @staticmethod
    def _build_candidate(existing: VaultDocument, summary: str) -> NewVaultDocument:
        """The row as it would be with the summary filled in.

        Every field but ``summary`` comes from the stored row, which is what
        makes this a one-field write rather than a replacement: there is no
        request field that could reach the body or the title even by mistake.
        Built as a ``NewVaultDocument`` for the same reason the update path
        does it -- so the candidate satisfies exactly the ``validate`` and
        ``assemble_embedding_text`` every other write path runs.
        """

        return NewVaultDocument(
            id=existing.id,
            kind=existing.kind,
            doc_type=existing.doc_type,
            vault_path=existing.vault_path,
            status=existing.status,
            doc_status=existing.doc_status,
            title=existing.title,
            summary=summary,
            body=existing.body,
            tags=existing.tags,
            aliases=existing.aliases,
            facets=existing.facets,
            related_ids=existing.related_ids,
            source_ids=existing.source_ids,
            contributed_by=existing.contributed_by,
            source_url=existing.source_url,
            provenance=existing.provenance,
        )


class AmendmentProposalNotFound(Exception):
    """No amendment proposal carries that id."""


class AmendmentProposalAlreadyDecided(Exception):
    """The proposal was settled before this decision reached it."""


class AmendmentBaseRevisionMismatch(Exception):
    """The proposed base is not the target's current content revision."""


class AmendmentRemovalAcknowledgementRequired(Exception):
    """Acceptance omitted the explicit acknowledgement required for removals."""


@dataclass(frozen=True, slots=True)
class SpanEdit:
    """An exact old span and what to put in its place.

    The alternative authoring form for a body change. It never reaches
    storage: `propose` converts it to the canonical unified diff before
    anything is written, so the proposal table needs no new kind and a
    reviewer reads the same artifact either way. See ADR 0033.
    """

    expected_text: str
    replacement_text: str
    # None means "this span must be unique", which is the safe reading of a
    # caller who has not thought about duplicates. An integer is that caller
    # saying they have.
    occurrence: int | None = None


@dataclass(frozen=True, slots=True)
class MetadataChange:
    """The fields a metadata amendment may set, each optional.

    ``None`` means "leave this alone" and an empty collection means "make this
    empty", which is the distinction a caller needs in order to clear a field
    without resending the ones it is not touching.

    Deliberately excludes ``tags`` and ``aliases``. Both join
    ``assemble_embedding_text``, so changing either is a change to what the note
    means to search -- it needs a re-embed and a dedup run, and it can collide.
    Excluding them is what lets this kind promise that it cannot alter
    retrieval, which is the promise the whole operation is built on. See vault
    ADR 0036.
    """

    related_ids: tuple[str, ...] | None = None
    source_ids: tuple[str, ...] | None = None
    facets: dict[str, list[str]] | None = None
    source_url: str | None = None
    # `source_url` is nullable in storage, so "set it to null" and "leave it
    # alone" cannot both be spelled `None`. This says which was meant.
    clear_source_url: bool = False

    def is_empty(self) -> bool:
        return (
            self.related_ids is None
            and self.source_ids is None
            and self.facets is None
            and self.source_url is None
            and not self.clear_source_url
        )


@dataclass(frozen=True, slots=True)
class AmendmentProposalRequest:
    target_document_id: str
    base_revision: int
    change_kind: AmendmentProposalKind
    rationale: str
    principal_id: str
    request_id: str
    replacement: UpdateRequest | None = None
    body_diff: str | None = None
    # Resolved to `body_diff` against the loaded target inside `propose`, and
    # never persisted. Carried on the request rather than converted in the
    # adapter because the conversion needs the note's current body, which only
    # the service is allowed to read.
    span: SpanEdit | None = None
    metadata: MetadataChange | None = None


@dataclass(frozen=True, slots=True)
class AmendmentDecisionRequest:
    proposal_id: UUID
    state: AmendmentProposalState
    principal_id: str
    request_id: str
    decision_note: str | None = None
    acknowledge_removals: bool = False


@dataclass(frozen=True, slots=True)
class AmendmentDecisionOutcome:
    proposal: VaultAmendmentProposal
    outcome: str
    target: VaultDocument | None


@dataclass(frozen=True, slots=True)
class MetadataUpdateRequest:
    """Apply a metadata change directly, for a caller holding update authority."""

    document_id: str
    base_revision: int
    change: MetadataChange
    principal_id: str
    request_id: str


class VaultDocumentMetadataService:
    """Change a note's edges and classification without touching its content.

    The applied counterpart of the `metadata` amendment kind (ADR 0036), and
    deliberately *not* a thin wrapper over `VaultDocumentUpdateService`. That
    service embeds outside the transaction, takes the corpus lock, and runs the
    dedup gate, because an update can create a duplicate. None of that applies
    here: every field this accepts is excluded from `assemble_embedding_text`,
    so the note's embedding text is unchanged by construction, its vector still
    describes it, and it cannot have become a duplicate of anything.

    Skipping the gate is therefore a consequence of the payload rather than an
    exemption granted to a caller — which is why the permitted keys are fixed
    in the schema and not merely in this class.
    """

    def __init__(self, transactions: VaultTransactionService) -> None:
        self._transactions = transactions

    async def update(self, request: MetadataUpdateRequest) -> VaultDocument:
        if request.change.is_empty():
            raise ValueError("a metadata update must change at least one field")

        documents = VaultDocumentRepository()
        async with self._transactions.transaction() as connection:
            # The corpus lock is still taken. Nothing here can collide, but the
            # write is a read-modify-write against a row other governed writers
            # also touch, and the revision check below is only as good as the
            # window it runs in.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            target = await documents.get_by_id(
                connection,
                request.document_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )
            if target is None:
                raise DocumentNotFound(request.document_id)
            if target.content_revision != request.base_revision:
                raise AmendmentBaseRevisionMismatch(
                    f"{request.document_id} is at revision "
                    f"{target.content_revision}, not {request.base_revision}"
                )

            update_request = VaultAmendmentService._metadata_update(
                request.change,
                target,
                principal_id=request.principal_id,
                request_id=request.request_id,
            )
            candidate = VaultDocumentUpdateService._build_candidate(
                target, update_request
            )
            errors = validate(candidate) + validate_facets(candidate.facets)
            if errors:
                raise ValueError("; ".join(errors))

            updated = await documents.replace_content(
                connection,
                request.document_id,
                candidate,
                expected_revision=request.base_revision,
            )
            if updated is None:
                raise AmendmentBaseRevisionMismatch(
                    f"{request.document_id} changed while this update was in flight"
                )

            await VaultAuditEventRepository().record(
                connection,
                operation="vault.update_metadata",
                outcome="updated",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=request.document_id,
            )
        return updated


class VaultAmendmentService:
    """Submit and adjudicate immutable, revision-bound note changes.

    Proposal submission does not touch the searchable corpus and therefore does
    not embed or deduplicate. Acceptance runs the same validation, embedding,
    deduplication, advisory-lock and vector-persistence rules as direct update,
    then settles the proposal in that same transaction.
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

    async def propose(self, request: AmendmentProposalRequest) -> VaultAmendmentProposal:
        documents = VaultDocumentRepository()
        proposals = VaultAmendmentProposalRepository()

        async with self._transactions.transaction() as connection:
            # Serialize the base-revision check with every content mutation.
            # Without the lock, a direct update could commit between this read
            # and the proposal insert, creating a proposal already stale while
            # returning success to its author.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            target = await documents.get_by_id(
                connection,
                request.target_document_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )
            if target is None or target.kind is not DocumentKind.NOTE:
                raise DocumentNotFound(request.target_document_id)
            if target.content_revision != request.base_revision:
                raise AmendmentBaseRevisionMismatch(request.target_document_id)

            try:
                # Inside the guard on purpose: a span that matches nothing or
                # matches twice raises SpanEditError, which is a BodyDiffError,
                # and reaches the caller as the same client error a malformed
                # patch would.
                resolved = self._materialize_span(request, target)
                update_request = self._request_update(resolved, target)
                candidate = VaultDocumentUpdateService._build_candidate(
                    target, update_request
                )
            except (BodyDiffError, FacetNameCollision) as exc:
                raise ValueError(str(exc)) from exc
            errors = validate(candidate) + validate_facets(candidate.facets)
            if errors:
                raise ValueError("; ".join(errors))
            rationale = request.rationale.strip()
            if not rationale:
                raise ValueError("rationale must contain non-whitespace text")

            change = self._change_payload(resolved)
            proposal = await proposals.insert_pending(
                connection,
                target_document_id=target.id,
                target_revision=target.content_revision,
                # `resolved`, not `request`: a span edit is materialized into a
                # body diff above, so the pre-resolution kind would label a
                # body-diff payload as whatever the caller sent. The MCP
                # adapter happens to send BODY_DIFF, which is why this was
                # invisible -- but the stored shape must not depend on an
                # adapter getting a redundant field right.
                change_kind=resolved.change_kind,
                change=change,
                rationale=rationale,
                proposed_by=f"agent:{request.principal_id}",
            )
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.amendment.propose",
                outcome="pending",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="amendment_proposal",
                target_id=str(proposal.id),
            )
            return proposal

    async def list_pending(self, limit: int = 50) -> tuple[VaultAmendmentProposal, ...]:
        async with self._transactions.transaction() as connection:
            return await VaultAmendmentProposalRepository().list_pending(
                connection, limit
            )

    async def get(
        self, proposal_id: UUID
    ) -> tuple[VaultAmendmentProposal, VaultDocument | None]:
        async with self._transactions.transaction() as connection:
            proposal = await VaultAmendmentProposalRepository().get(
                connection, proposal_id
            )
            if proposal is None:
                raise AmendmentProposalNotFound(str(proposal_id))
            target = await VaultDocumentRepository().get_by_id(
                connection, proposal.target_document_id
            )
            return proposal, target

    def preview(
        self,
        proposal: VaultAmendmentProposal,
        target: VaultDocument | None,
    ) -> BodyChangeSummary | None:
        """Materialize a review preview only against the proposal's exact base."""

        if target is None or target.content_revision != proposal.target_revision:
            return None
        update_request = self._update_request(
            proposal,
            target,
            principal_id="preview",
            request_id="preview",
        )
        candidate = VaultDocumentUpdateService._build_candidate(target, update_request)
        return summarize_body_change(target.body, candidate.body)

    async def decide(
        self, request: AmendmentDecisionRequest
    ) -> AmendmentDecisionOutcome:
        if request.state is AmendmentProposalState.PENDING:
            raise ValueError("a decision cannot leave a proposal pending")
        if request.state is AmendmentProposalState.STALE:
            raise ValueError("stale is determined by the service")
        if request.state is AmendmentProposalState.REJECTED:
            return await self._reject(request)
        return await self._accept(request)

    async def _reject(
        self, request: AmendmentDecisionRequest
    ) -> AmendmentDecisionOutcome:
        proposals = VaultAmendmentProposalRepository()
        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            existing = await proposals.get(connection, request.proposal_id)
            if existing is None:
                raise AmendmentProposalNotFound(str(request.proposal_id))
            decided = await proposals.decide(
                connection,
                request.proposal_id,
                state=AmendmentProposalState.REJECTED,
                decided_by=f"agent:{request.principal_id}",
                decision_note=request.decision_note,
            )
            if decided is None:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
            await self._audit_decision(connection, request, "rejected")
            target = await VaultDocumentRepository().get_by_id(
                connection, existing.target_document_id
            )
            return AmendmentDecisionOutcome(decided, "rejected", target)

    async def _accept(
        self, request: AmendmentDecisionRequest
    ) -> AmendmentDecisionOutcome:
        proposals = VaultAmendmentProposalRepository()
        documents = VaultDocumentRepository()
        embeddings = VaultDocumentEmbeddingRepository()

        async with self._transactions.transaction() as connection:
            proposal = await proposals.get(connection, request.proposal_id)
            if proposal is None:
                raise AmendmentProposalNotFound(str(request.proposal_id))
            if proposal.state is not AmendmentProposalState.PENDING:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
            target = await documents.get_by_id(
                connection, proposal.target_document_id
            )
            preflight_stale = (
                target is None
                or target.content_revision != proposal.target_revision
            )
            stored = (
                await embeddings.get(
                    connection, target.id, self._provider.profile_id
                )
                if not preflight_stale and self._provider is not None
                else None
            )

        if preflight_stale:
            return await self._settle_stale(request, proposal)
        if target is None:
            raise AssertionError("a non-stale amendment target must exist")

        update_request = self._update_request(
            proposal,
            target,
            principal_id=request.principal_id,
            request_id=request.request_id,
        )
        candidate = VaultDocumentUpdateService._build_candidate(target, update_request)
        body_summary = summarize_body_change(target.body, candidate.body)
        if (
            body_summary.requires_removal_acknowledgement
            and not request.acknowledge_removals
        ):
            raise AmendmentRemovalAcknowledgementRequired(
                "acceptance removes existing lines; set acknowledge_removals=true "
                "after reviewing the removal summary"
            )
        # A metadata acceptance needs no provider and must not embed.
        #
        # Every field that kind can carry is excluded from the embedding text,
        # so the note's stored vector still describes it and it cannot have
        # become a duplicate of anything -- there is nothing for the gate to
        # decide. Falling through to the shared path made ADR 0036's promise
        # false in two ordinary states: a deployment with no provider refused
        # the write with 503, and a note that happened to have no embedding row
        # got one written from this change, which is precisely the "cannot
        # affect retrieval" claim failing.
        if proposal.change_kind is AmendmentProposalKind.METADATA:
            errors = validate(candidate) + validate_facets(candidate.facets)
            if errors:
                raise ValueError("; ".join(errors))
            return await self._settle_metadata_acceptance(
                request,
                proposal=proposal,
                target=target,
                candidate=candidate,
            )

        if self._provider is None:
            raise DedupUnavailable(
                "No embedding provider is configured; refusing to write without dedup"
            )
        errors = validate(candidate) + validate_facets(candidate.facets)
        if errors:
            raise ValueError("; ".join(errors))

        embedding_text = assemble_embedding_text(candidate)
        text_digest = embedding_text_digest(embedding_text)
        re_embed = stored is None or stored.text_sha256 != text_digest
        vector = (
            await embed_one(
                self._provider, embedding_text, EmbeddingInputKind.DOCUMENT
            )
            if re_embed
            else stored.vector
        )

        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            current_proposal = await proposals.get(connection, request.proposal_id)
            if current_proposal is None:
                raise AmendmentProposalNotFound(str(request.proposal_id))
            if current_proposal.state is not AmendmentProposalState.PENDING:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))

            current = await documents.get_by_id(
                connection, current_proposal.target_document_id
            )
            if (
                current is None
                or current.content_revision != current_proposal.target_revision
            ):
                decided = await proposals.decide(
                    connection,
                    request.proposal_id,
                    state=AmendmentProposalState.STALE,
                    decided_by=f"agent:{request.principal_id}",
                    decision_note=request.decision_note,
                )
                if decided is None:
                    raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
                await self._audit_decision(connection, request, "stale")
                return AmendmentDecisionOutcome(decided, "stale", current)

            similars = await VaultSearchRepository().find_similar(
                connection,
                embedding=vector,
                profile_id=self._provider.profile_id,
                limit=self._similar_limit,
                exclude_document_id=current.id,
            )
            match decide(candidate, similars, self._policy):
                case Insert():
                    pass
                case Flag(reason=reason, similars=sims):
                    raise UpdateWouldDuplicate(f"amendment refused: {reason}", sims)
                case Reject(reason=reason, conflicting_id=conflicting):
                    raise UpdateWouldDuplicate(
                        f"amendment refused: {reason} (conflicts with {conflicting})",
                        similars,
                    )
                case Link():
                    raise NotImplementedError("Link is disabled under the current policy")
                case Merge():
                    raise NotImplementedError("Merge is disabled under the current policy")

            updated = await documents.replace_content(
                connection,
                current.id,
                candidate,
                expected_revision=current_proposal.target_revision,
            )
            if updated is None:
                raise AmendmentBaseRevisionMismatch(current.id)
            await embeddings.upsert(
                connection,
                DocumentEmbedding(
                    document_id=current.id,
                    profile_id=self._provider.profile_id,
                    vector=vector,
                    text_sha256=text_digest,
                ),
            )
            decided = await proposals.decide(
                connection,
                request.proposal_id,
                state=AmendmentProposalState.ACCEPTED,
                decided_by=f"agent:{request.principal_id}",
                decision_note=request.decision_note,
                applied_revision=updated.content_revision,
                removals_acknowledged=(
                    body_summary.requires_removal_acknowledgement
                    and request.acknowledge_removals
                ),
            )
            if decided is None:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
            await self._audit_decision(connection, request, "accepted")
            return AmendmentDecisionOutcome(decided, "accepted", updated)

    async def _settle_metadata_acceptance(
        self,
        request: AmendmentDecisionRequest,
        *,
        proposal: VaultAmendmentProposal,
        target: VaultDocument,
        candidate: NewVaultDocument,
    ) -> AmendmentDecisionOutcome:
        """Apply an accepted metadata proposal without touching the index.

        The same sequence the ordinary acceptance runs -- lock, re-read, verify
        the revision, write under a CAS, settle the proposal, audit -- with the
        embedding, vector upsert, similarity search, and dedup decision left
        out rather than made conditional. They are absent because nothing this
        kind carries reaches `assemble_embedding_text`, so there is no vector
        to refresh and no duplicate that could have been created.

        Consequences worth stating: this path works with no embedding provider
        configured, and it leaves a note that has no embedding row exactly as
        it found it. Both were failures before -- 503 in the first case, an
        unrelated vector written in the second.
        """

        proposals = VaultAmendmentProposalRepository()
        documents = VaultDocumentRepository()

        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            current_proposal = await proposals.get(connection, request.proposal_id)
            if current_proposal is None:
                raise AmendmentProposalNotFound(str(request.proposal_id))
            if current_proposal.state is not AmendmentProposalState.PENDING:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))

            current = await documents.get_by_id(connection, target.id)
            if (
                current is None
                or current.content_revision != current_proposal.target_revision
            ):
                decided = await proposals.decide(
                    connection,
                    request.proposal_id,
                    state=AmendmentProposalState.STALE,
                    decided_by=f"agent:{request.principal_id}",
                    decision_note=request.decision_note,
                )
                if decided is None:
                    raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
                await self._audit_decision(connection, request, "stale")
                return AmendmentDecisionOutcome(decided, "stale", current)

            updated = await documents.replace_content(
                connection,
                current.id,
                candidate,
                expected_revision=current_proposal.target_revision,
            )
            if updated is None:
                raise AmendmentBaseRevisionMismatch(current.id)

            decided = await proposals.decide(
                connection,
                request.proposal_id,
                state=AmendmentProposalState.ACCEPTED,
                decided_by=f"agent:{request.principal_id}",
                decision_note=request.decision_note,
                applied_revision=updated.content_revision,
                # A metadata change removes no body lines, so there is nothing
                # for a reviewer to acknowledge.
                removals_acknowledged=False,
            )
            if decided is None:
                raise AmendmentProposalAlreadyDecided(str(request.proposal_id))
            await self._audit_decision(connection, request, "accepted")
            return AmendmentDecisionOutcome(decided, "accepted", updated)

    async def _settle_stale(
        self,
        request: AmendmentDecisionRequest,
        proposal: VaultAmendmentProposal,
    ) -> AmendmentDecisionOutcome:
        proposals = VaultAmendmentProposalRepository()
        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            current = await proposals.get(connection, proposal.id)
            if current is None:
                raise AmendmentProposalNotFound(str(proposal.id))
            decided = await proposals.decide(
                connection,
                proposal.id,
                state=AmendmentProposalState.STALE,
                decided_by=f"agent:{request.principal_id}",
                decision_note=request.decision_note,
            )
            if decided is None:
                raise AmendmentProposalAlreadyDecided(str(proposal.id))
            await self._audit_decision(connection, request, "stale")
            target = await VaultDocumentRepository().get_by_id(
                connection, proposal.target_document_id
            )
            return AmendmentDecisionOutcome(decided, "stale", target)

    @staticmethod
    def _replacement_payload(request: UpdateRequest) -> dict[str, object]:
        return {
            "title": request.title,
            "body": request.body,
            "summary": request.summary,
            "tags": list(request.tags),
            "aliases": list(request.aliases),
            "facets": request.facets,
            "related_ids": list(request.related_ids),
            "source_ids": list(request.source_ids),
            "source_url": request.source_url,
        }

    @staticmethod
    def _materialize_span(
        request: AmendmentProposalRequest,
        target: VaultDocument,
    ) -> AmendmentProposalRequest:
        """Turn a span edit into the diff that will be stored, or pass through.

        Runs after the base-revision check and under the same advisory lock,
        which is what makes the span meaningful: it is resolved against the
        exact body the caller claimed to be editing, so a concurrent write
        cannot move the text out from under it between check and conversion.
        """

        if request.span is None:
            return request
        if request.body_diff is not None or request.replacement is not None:
            raise ValueError("a span edit carries neither body_diff nor replacement")
        # The kind a span resolves to is BODY_DIFF and nothing else, so a
        # request arriving with a contradictory one is a caller that disagrees
        # with this function about what it is building. Refuse rather than
        # quietly overwrite it: the stored payload and its label have to be
        # decided in the same place.
        if request.change_kind is not AmendmentProposalKind.BODY_DIFF:
            raise ValueError(
                "a span edit resolves to a body diff; change_kind "
                f"{request.change_kind.value!r} contradicts the span"
            )

        return replace(
            request,
            change_kind=AmendmentProposalKind.BODY_DIFF,
            body_diff=span_edit_to_unified_diff(
                target.body,
                expected_text=request.span.expected_text,
                replacement_text=request.span.replacement_text,
                occurrence=request.span.occurrence,
            ),
            span=None,
        )

    @classmethod
    def _metadata_payload(cls, change: MetadataChange) -> dict[str, object]:
        """Only the fields the caller actually set.

        Omitting the untouched ones is what makes the stored proposal readable:
        a reviewer sees the change rather than a whole document with four
        differences in it.
        """

        payload: dict[str, object] = {}
        if change.related_ids is not None:
            payload["related_ids"] = list(change.related_ids)
        if change.source_ids is not None:
            payload["source_ids"] = list(change.source_ids)
        if change.facets is not None:
            payload["facets"] = dict(change.facets)
        if change.clear_source_url:
            payload["source_url"] = None
        elif change.source_url is not None:
            payload["source_url"] = change.source_url
        if not payload:
            raise ValueError("a metadata proposal must change at least one field")
        return payload

    @staticmethod
    def _metadata_update(
        change: MetadataChange,
        target: VaultDocument,
        *,
        principal_id: str,
        request_id: str,
    ) -> UpdateRequest:
        """The target with only the named metadata fields replaced.

        Everything else comes from the stored row, which is what makes this a
        metadata edit rather than a replacement that happens to keep most
        fields: there is no request field that could reach the title or body
        even by mistake.
        """

        source_url = target.source_url
        if change.clear_source_url:
            source_url = None
        elif change.source_url is not None:
            source_url = change.source_url

        return UpdateRequest(
            document_id=target.id,
            title=target.title,
            body=target.body,
            principal_id=principal_id,
            request_id=request_id,
            summary=target.summary,
            tags=target.tags,
            aliases=target.aliases,
            facets=dict(change.facets) if change.facets is not None else target.facets,
            related_ids=(
                tuple(change.related_ids)
                if change.related_ids is not None
                else target.related_ids
            ),
            source_ids=(
                tuple(change.source_ids)
                if change.source_ids is not None
                else target.source_ids
            ),
            source_url=source_url,
        )

    @classmethod
    def _change_payload(cls, request: AmendmentProposalRequest) -> dict[str, object]:
        if request.change_kind is AmendmentProposalKind.METADATA:
            if request.metadata is None:
                raise ValueError("metadata proposals require a metadata change")
            if request.replacement is not None or request.body_diff is not None:
                raise ValueError(
                    "a metadata proposal carries neither replacement nor body_diff"
                )
            return cls._metadata_payload(request.metadata)
        if request.change_kind is AmendmentProposalKind.REPLACEMENT:
            if request.replacement is None or request.body_diff is not None:
                raise ValueError("replacement proposals require only replacement content")
            return cls._replacement_payload(request.replacement)
        if request.body_diff is None or request.replacement is not None:
            raise ValueError("body-diff proposals require only body_diff")
        return {"body_diff": request.body_diff}

    @classmethod
    def _request_update(
        cls,
        request: AmendmentProposalRequest,
        target: VaultDocument,
    ) -> UpdateRequest:
        if request.change_kind is AmendmentProposalKind.METADATA:
            if request.metadata is None:
                raise ValueError("metadata proposals require a metadata change")
            return cls._metadata_update(
                request.metadata,
                target,
                principal_id=request.principal_id,
                request_id=request.request_id,
            )
        if request.change_kind is AmendmentProposalKind.REPLACEMENT:
            if request.replacement is None or request.body_diff is not None:
                raise ValueError("replacement proposals require only replacement content")
            return request.replacement
        if request.body_diff is None or request.replacement is not None:
            raise ValueError("body-diff proposals require only body_diff")
        result = apply_body_unified_diff(target.body, request.body_diff)
        return cls._body_only_update(
            target,
            result.body,
            principal_id=request.principal_id,
            request_id=request.request_id,
        )

    @staticmethod
    def _body_only_update(
        target: VaultDocument,
        body: str,
        *,
        principal_id: str,
        request_id: str,
    ) -> UpdateRequest:
        return UpdateRequest(
            document_id=target.id,
            title=target.title,
            body=body,
            principal_id=principal_id,
            request_id=request_id,
            summary=target.summary,
            tags=target.tags,
            aliases=target.aliases,
            facets=target.facets,
            related_ids=target.related_ids,
            source_ids=target.source_ids,
            source_url=target.source_url,
        )

    @staticmethod
    def _update_request(
        proposal: VaultAmendmentProposal,
        target: VaultDocument,
        *,
        principal_id: str,
        request_id: str,
    ) -> UpdateRequest:
        if proposal.change_kind is AmendmentProposalKind.METADATA:
            stored = proposal.change
            change = MetadataChange(
                related_ids=(
                    tuple(stored["related_ids"]) if "related_ids" in stored else None
                ),
                source_ids=(
                    tuple(stored["source_ids"]) if "source_ids" in stored else None
                ),
                facets=dict(stored["facets"]) if "facets" in stored else None,
                source_url=stored.get("source_url"),
                clear_source_url=(
                    "source_url" in stored and stored["source_url"] is None
                ),
            )
            return VaultAmendmentService._metadata_update(
                change,
                target,
                principal_id=principal_id,
                request_id=request_id,
            )
        if proposal.change_kind is AmendmentProposalKind.BODY_DIFF:
            patch = proposal.change.get("body_diff")
            if not isinstance(patch, str):
                raise ValueError("stored body-diff proposal is malformed")
            try:
                body = apply_body_unified_diff(target.body, patch).body
            except BodyDiffError as exc:
                raise ValueError(str(exc)) from exc
            return VaultAmendmentService._body_only_update(
                target,
                body,
                principal_id=principal_id,
                request_id=request_id,
            )
        content = proposal.change
        return UpdateRequest(
            document_id=proposal.target_document_id,
            title=str(content["title"]),
            body=str(content["body"]),
            principal_id=principal_id,
            request_id=request_id,
            summary=content.get("summary"),
            tags=tuple(content.get("tags", ())),
            aliases=tuple(content.get("aliases", ())),
            facets=dict(content.get("facets", {})),
            related_ids=tuple(content.get("related_ids", ())),
            source_ids=tuple(content.get("source_ids", ())),
            source_url=content.get("source_url"),
        )

    @staticmethod
    async def _audit_decision(
        connection: AsyncConnection,
        request: AmendmentDecisionRequest,
        outcome: str,
    ) -> None:
        await VaultAuditEventRepository().record(
            connection,
            operation="vault.amendment.review",
            outcome=outcome,
            request_id=request.request_id,
            principal_id=request.principal_id,
            target_type="amendment_proposal",
            target_id=str(request.proposal_id),
        )

@dataclass(frozen=True, slots=True)
class RetireRequest:
    """A request to remove one document."""

    document_id: str
    principal_id: str
    request_id: str


class DocumentUnderReview(Exception):
    """A review case prevents retiring this candidate or pending evidence."""


class VaultDocumentRetireService:
    """Remove a document from the vault.

    Deletion rather than an archived status, and that is the decision worth
    stating. ADR 0008 keeps archived documents out of search but still
    resolvable by id, which is right for content that is *retired* -- superseded
    but true. The case this exists for is content that is **wrong**, where a row
    a caller can still resolve is the failure rather than the record.

    No dedup gate: retiring is the one write that cannot create a duplicate.
    """

    def __init__(self, transactions: VaultTransactionService) -> None:
        self._transactions = transactions

    async def retire(self, request: RetireRequest) -> None:
        documents = VaultDocumentRepository()

        async with self._transactions.transaction() as connection:
            # Contributions create review evidence under this same corpus lock.
            # Retirement must serialize with that write or it can check first,
            # delete an evidence document, and let the pending case commit with
            # a dangling JSON reference immediately afterwards.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            existing = await documents.get_by_id(
                connection,
                request.document_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )
            if existing is None:
                raise DocumentNotFound(request.document_id)

            # A candidate reference is a durable, non-cascading FK in every
            # state. Similar-document evidence is JSON without an FK and blocks
            # while pending, when deleting it would destroy unresolved context.
            blocking_cases = (
                await documents.count_retirement_blocking_review_references(
                    connection, request.document_id
                )
            )
            if blocking_cases:
                raise DocumentUnderReview(
                    f"{blocking_cases} review case(s) reference this document"
                )

            # Audit first: the event has to survive the row it describes, and
            # writing it inside the same transaction means a retirement can
            # never be observed without its record, or the reverse.
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.retire",
                outcome="retired",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=request.document_id,
            )
            removed = await documents.delete(connection, request.document_id)
            if not removed:
                raise DocumentNotFound(request.document_id)


class ReviewCaseNotFound(Exception):
    """No review case carries that id."""


class ReviewCaseAlreadyDecided(Exception):
    """The case was settled before this decision reached it."""


@dataclass(frozen=True, slots=True)
class ReviewDecisionRequest:
    review_case_id: UUID
    state: ReviewState
    principal_id: str
    request_id: str
    decision_note: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewDecisionOutcome:
    """What the decision did, in the terms the caller reports."""

    review_case: VaultReviewCase
    # The candidate's fate: ``published`` for an accepted note now serving from
    # search, ``deleted`` for a rejected duplicate, ``absent`` when the
    # candidate was already gone.
    candidate: str


class VaultReviewService:
    """Adjudicate the near-duplicate cases the write path opened.

    **A candidate is always a brand-new note.** ``insert_pending`` is called
    from one place -- the contribute path's ``Flag`` branch -- with the note it
    has just written; the notes it resembles appear only as evidence, and the
    update path refuses on collision rather than opening a case. Every decision
    is therefore about content that has never been endorsed, which is what makes
    the two outcomes what they are:

    - ``ACCEPTED`` -- the flag was a false positive. The note goes ``active``
      and rejoins search and the dedup corpus.
    - ``REJECTED`` -- it really is a duplicate. The note is **deleted**, per ADR
      0019's line between archiving what is overtaken and deleting what is
      wrong. A duplicate judged redundant at birth has no history worth keeping,
      and its content is by definition already in the corpus: that is what the
      case said.

    The case survives either way. On a rejection the candidate pointer goes null
    (migration 0011) rather than the judgement going with the note.

    Reading a case means serving ``flagged`` content, which is the least-vetted
    text in the corpus. That is why this is a REST-only surface gated on
    ``vault:review`` and deliberately absent from the MCP tool list: ADR 0021's
    defence against injected instructions is the privileged tool not being there
    to name.
    """

    def __init__(self, transactions: VaultTransactionService) -> None:
        self._transactions = transactions

    async def list_pending(self, limit: int = 50) -> tuple[VaultReviewCase, ...]:
        async with self._transactions.transaction() as connection:
            return await VaultReviewCaseRepository().list_pending(connection, limit)

    async def get(
        self,
        review_case_id: UUID,
    ) -> tuple[VaultReviewCase, VaultDocument | None]:
        """One case and its candidate, which may be gone.

        The document is fetched unfiltered on purpose. ``READABLE_STATUSES``
        withholds ``flagged`` from the read surface (ADR 0008), and a reviewer
        needs precisely the content that rule hides -- adjudicating a note you
        cannot read is not a review. The restriction stays at the public
        surface; this one is gated on its own scope instead.
        """

        async with self._transactions.transaction() as connection:
            case = await VaultReviewCaseRepository().get(connection, review_case_id)
            if case is None:
                raise ReviewCaseNotFound(str(review_case_id))
            candidate = (
                await VaultDocumentRepository().get_by_id(
                    connection, case.candidate_document_id
                )
                if case.candidate_document_id is not None
                else None
            )
            return case, candidate

    async def decide(self, request: ReviewDecisionRequest) -> ReviewDecisionOutcome:
        cases = VaultReviewCaseRepository()
        documents = VaultDocumentRepository()

        async with self._transactions.transaction() as connection:
            # The same corpus lock contribution and retirement take. A decision
            # both settles the case and moves the document, and a concurrent
            # retirement checks for pending cases -- without serializing, a
            # retire could observe no pending case while this one is mid-flight.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )

            existing = await cases.get(connection, request.review_case_id)
            if existing is None:
                raise ReviewCaseNotFound(str(request.review_case_id))

            decided = await cases.decide(
                connection,
                request.review_case_id,
                state=request.state,
                # From the credential, never the body: a reviewer must not be
                # able to sign someone else's name to a judgement.
                decided_by=f"agent:{request.principal_id}",
                decision_note=request.decision_note,
            )
            if decided is None:
                # The row exists but was not pending, so another reviewer
                # settled it first. Reported rather than silently overwritten.
                raise ReviewCaseAlreadyDecided(str(request.review_case_id))

            candidate_id = existing.candidate_document_id
            candidate = "absent"
            if candidate_id is not None:
                if request.state is ReviewState.ACCEPTED:
                    published = await documents.set_status(
                        connection,
                        candidate_id,
                        status=DocumentStatus.ACTIVE,
                        doc_status="Active",
                    )
                    candidate = "published" if published is not None else "absent"
                elif request.state is ReviewState.REJECTED:
                    # Audit before the delete, in the same transaction: the
                    # event outlives its subject and must never be observable
                    # without the deletion, or the reverse. See ADR 0019.
                    await VaultAuditEventRepository().record(
                        connection,
                        operation="vault.review",
                        outcome="candidate_deleted",
                        request_id=request.request_id,
                        principal_id=request.principal_id,
                        target_type="document",
                        target_id=candidate_id,
                    )
                    removed = await documents.delete(connection, candidate_id)
                    candidate = "deleted" if removed else "absent"

            await VaultAuditEventRepository().record(
                connection,
                operation="vault.review",
                outcome=request.state.value,
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="review_case",
                target_id=str(request.review_case_id),
            )
            # Re-read: the UPDATE above has to run first, because its
            # pending-state filter is the concurrency guard, but deleting a
            # rejected candidate then clears the case's pointer underneath it.
            # Returning the row as it was mid-transaction would report a
            # candidate that no longer exists.
            settled = await cases.get(connection, request.review_case_id)
            return ReviewDecisionOutcome(
                review_case=settled if settled is not None else decided,
                candidate=candidate,
            )


class PromotionNotApplicable(Exception):
    """The document cannot carry a promotion judgement.

    Raised for a document living outside the folders this verb routes between,
    which is the one way it could relocate a row into a tree the service does
    not own.
    """


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    """A reviewer's judgement about whether a note belongs in the Human layer."""

    document_id: str
    # None clears the judgement back to "never proposed". Distinct from
    # RETRACTED, which records that someone looked and declined.
    promotion_status: PromotionStatus | None
    principal_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    document: VaultDocument
    # Whether `vault_path` actually moved. False for a no-op re-application and
    # for any transition between two non-candidate states, which share a
    # folder.
    moved: bool


class VaultPromotionService:
    """Set a document's promotion candidacy, and move its file to match.

    ADR 0023: candidacy is a field, and the export projects it into a folder.
    The projection is ``vault_path`` -- ADR 0010 requires that column to stay
    byte-identical to the governance scanner's ``rel_path``, so the row has to
    name the folder the file is actually in or the wrong ``folders.yml`` rule
    resolves against it. Setting the field and moving the path is therefore one
    operation, not two.

    **The corpus lock is required, for the reason contribution takes it.**
    ``vault_path`` is UNIQUE and collisions suffix, so the free name is only
    still free while the lock is held. A promotion racing a contribution of a
    note with the same title is exactly the collision ``resolve_vault_path``
    exists to settle.

    **Active documents only.** ADR 0023 makes a candidate "a first-class agent
    note: served to agents, returned by search, and inside the dedup gate",
    which is a description of ``active`` and of nothing else. A flagged note is
    one the write path declined to endorse and an archived one is retired;
    projecting either into a folder that means *elevated* would put a file in
    front of a librarian for content agents cannot see. A flagged note is
    promotable once its review case is accepted, which is the right order.

    **No dedup gate, and no re-embedding.** Nothing about the content changes
    -- not the title, not the body, not ``updated_at`` -- so the embedding is
    still current and the rendered file is byte-identical either side of the
    move. That is what makes git show a rename and follow the history.
    """

    def __init__(self, transactions: VaultTransactionService) -> None:
        self._transactions = transactions

    async def set_promotion_status(
        self, request: PromotionRequest
    ) -> PromotionOutcome:
        documents = VaultDocumentRepository()

        async with self._transactions.transaction() as connection:
            # Serializes with contribution, retirement and review decisions --
            # all four resolve or invalidate a `vault_path`, and the answer to
            # "is this name taken" is only true under the lock.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )

            existing = await documents.get_by_id(
                connection,
                request.document_id,
                statuses=(DocumentStatus.ACTIVE,),
            )
            if existing is None:
                raise DocumentNotFound(request.document_id)

            home = _promotion_home_directory(existing.kind)
            if not existing.vault_path.startswith(
                (home, PROMOTION_CANDIDATES_DIRECTORY)
            ):
                # The verb routes between exactly two folders. A row anywhere
                # else -- an imported human note, a folder added later -- is
                # not something it may relocate, and refusing is the only
                # answer that cannot move a file into a tree this service does
                # not own.
                raise PromotionNotApplicable(
                    f"{existing.vault_path!r} is outside {home!r} and "
                    f"{PROMOTION_CANDIDATES_DIRECTORY!r}"
                )

            if existing.promotion_status is request.promotion_status:
                # Re-applying a judgement is a no-op rather than an error: it
                # must not re-resolve the path, which would suffix the note
                # against its own name and rewrite the file for nothing.
                return PromotionOutcome(document=existing, moved=False)

            directory = (
                PROMOTION_CANDIDATES_DIRECTORY
                if request.promotion_status is PromotionStatus.CANDIDATE
                else home
            )
            taken = await documents.vault_paths_under(connection, directory)
            # Its own path is not a collision with itself. Only reachable when
            # the folder is unchanged -- promoted/retracted/cleared all share
            # `home` -- and there the answer must be "stay put".
            taken.discard(existing.vault_path)
            resolved = resolve_vault_path(directory, existing.title, taken)

            updated = await documents.set_promotion_status(
                connection,
                request.document_id,
                promotion_status=request.promotion_status,
                vault_path=resolved,
            )
            if updated is None:
                # Present under the lock a moment ago. Nothing can delete it
                # while this transaction holds that lock, so this is a bug
                # rather than a race -- surfaced as the 404 it looks like from
                # outside.
                raise DocumentNotFound(request.document_id)

            await VaultAuditEventRepository().record(
                connection,
                operation="vault.promote",
                outcome=(
                    request.promotion_status.value
                    if request.promotion_status is not None
                    else "cleared"
                ),
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=request.document_id,
            )
            return PromotionOutcome(
                document=updated,
                moved=updated.vault_path != existing.vault_path,
            )


class CompileRunNotFound(Exception):
    """No compile run carries that id."""


class CompileRunNotYours(Exception):
    """The run was opened by a different principal.

    ``compiler_principal_id`` was recorded and never checked, so any holder of
    ``vault:compile`` could write pages into another principal's run and settle
    it. The run then names one compiler while its pages and its settlement audit
    events name another -- provenance that contradicts itself, which is the one
    thing a compile run exists to provide.

    A run is not a lock and this is not an authorization boundary: the scope
    already permits writing wiki pages, and a holder can open its own run
    whenever it likes. It is a consistency rule, which is why the refusal is
    409 rather than 403.
    """


class CompileRunAlreadySettled(Exception):
    """The run finished before this call reached it."""


class CompileTargetNotAPage(Exception):
    """``page_id`` names a document that is not a wiki page.

    A compile run rewrites pages it produced. Pointed at a note -- a stale id,
    or one copied out of the wrong column of a plan -- it would otherwise
    overwrite that note's body with synthesized content and only *then* fail,
    because compile provenance is wiki-only and would match no row. Refused
    before anything is written, and reported as a request error rather than a
    server fault: the id comes from the caller, and any credential holding
    ``vault:compile`` can send a wrong one.
    """

    def __init__(self, document_id: str, kind: DocumentKind) -> None:
        super().__init__(
            f"page_id {document_id} names a {kind.value}, not a wiki page"
        )
        self.document_id = document_id
        self.kind = kind


class UnresolvedSources(Exception):
    """A page cites note ids that do not resolve.

    Refused rather than stored, unlike ``related_ids``. ADR 0025 keeps edges
    opaque because a contribution may legitimately reference a note that is
    archived, flagged, or not yet written -- but a wiki page's ``source_ids``
    are its *provenance*, the record of what it was synthesized from, and
    provenance naming something that never existed is not a dangling edge, it
    is a false claim. The Stage-A compiler refuses the same way.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        super().__init__(f"unresolved source id(s): {', '.join(missing)}")
        self.missing = tuple(missing)


@dataclass(frozen=True, slots=True)
class CompilePlan:
    run: VaultCompileRun
    items: tuple[CompileWorkItem, ...]


@dataclass(frozen=True, slots=True)
class CompilePageRequest:
    """One page a compiling agent has written."""

    run_id: UUID
    title: str
    body: str
    source_ids: tuple[str, ...]
    principal_id: str
    request_id: str
    summary: str | None = None
    tags: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    # Replace this page rather than creating one. From the plan's `page_id`.
    page_id: str | None = None


class VaultCompileService:
    """Wiki compilation: plan a run, write its pages, settle it.

    **The service plans; the agent synthesizes.** This is the same division the
    Stage-A engine draws, and it is the only one that works: deciding *which*
    pages are stale is a query, and writing the prose that distils four notes
    into one page is not. So ``plan`` returns work items and ``write_page``
    accepts the result, with a model in between.

    **A plan carries note ids, never note bodies.** The agent fetches what it
    needs through the ordinary read surface, which is already policy-checked
    (ADR 0014). Inlining bodies would be a second read path with its own
    disclosure rules, and a response the size of the corpus.

    **No dedup gate.** A compiled page restates its sources by construction, so
    scoring it against them would flag every page ever written. That is not a
    hole in ADR 0016's "no dedup, no write" -- that rule is about notes
    duplicating notes, and ``find_similar`` now excludes wiki pages from the
    corpus for the same reason. Pages are still embedded, because search must
    return synthesis; they are simply not adjudicated as duplicates.

    **Provenance is a foreign key with ``ON DELETE RESTRICT``.** Every page
    names its run, and the run cannot be deleted while pages cite it (migration
    0008). ``source_ids`` are validated at write time and refused when
    unresolved, unlike a note's opaque edges -- provenance that names nothing
    is a false claim rather than a dangling reference.
    """

    def __init__(
        self,
        transactions: VaultTransactionService,
        provider: EmbeddingProvider | None,
    ) -> None:
        self._transactions = transactions
        self._provider = provider

    # ---------------------------------------------------------------- plan --

    async def plan(self, principal_id: str, all_pages: bool = False) -> CompilePlan:
        """Open a run and describe the work it should do.

        ``all_pages`` forces a full recompile, which is what the Stage-A engine
        calls a full flush and what an operator wants after changing the page
        model. Incremental is the default: only pages whose sources moved, and
        only notes written since the last run's frontier.
        """

        run_id = uuid4()
        documents = VaultWikiPageRepository()
        runs = VaultCompileRunRepository()

        async with self._transactions.transaction() as connection:
            # Still recorded on the run, and no longer read when planning.
            # `output_frontier` is the run's own history -- when the source
            # corpus stood where it did -- while what a note has been *judged*
            # to be now lives on the note.
            frontier = await runs.note_frontier(connection)
            pages = await documents.list_pages(connection)
            notes = await documents.note_states(connection)
            run = await runs.start(
                connection,
                run_id=run_id,
                compiler_principal_id=principal_id,
                input_frontier={"frontier_at": frontier} if frontier else {},
            )
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.compile",
                outcome="planned",
                request_id=uuid4().hex,
                principal_id=principal_id,
                target_type="compile_run",
                target_id=str(run_id),
            )

        items = _plan_items(pages=pages, notes=notes, all_pages=all_pages)
        return CompilePlan(run=run, items=items)

    # ---------------------------------------------------------- write page --

    async def write_page(self, request: CompilePageRequest) -> VaultDocument:
        """Store one compiled page, embedded and attributed to its run."""

        documents = VaultDocumentRepository()
        wiki = VaultWikiPageRepository()
        runs = VaultCompileRunRepository()
        embeddings = VaultDocumentEmbeddingRepository()

        if self._provider is None:
            # Same rule the write path applies: a page nobody can find is not
            # a page. Unlike dedup this is about the read surface, so it fails
            # rather than degrading.
            raise DedupUnavailable(
                "No embedding provider is configured; refusing to write an "
                "unsearchable wiki page"
            )

        async with self._transactions.transaction() as connection:
            run = await runs.get(connection, request.run_id)
            if run is None:
                raise CompileRunNotFound(str(request.run_id))
            if run.compiler_principal_id != request.principal_id:
                raise CompileRunNotYours(str(request.run_id))
            if run.state is not CompileRunState.RUNNING:
                raise CompileRunAlreadySettled(str(request.run_id))
            notes = await wiki.note_states(connection)

        missing = [
            source_id
            for source_id in request.source_ids
            if source_id not in notes
        ]
        if missing:
            raise UnresolvedSources(missing)

        candidate = self._build_page(request, run)
        errors = validate(candidate)
        if errors:
            raise ValueError("; ".join(errors))

        # Embed outside the transaction, for the reason the contribute path
        # does: an embedding call is a third-party round trip and holding a
        # pooled connection across it is the mistake this package keeps naming.
        text = assemble_embedding_text(candidate)
        digest = embedding_text_digest(text)
        vector = await embed_one(
            self._provider, text, EmbeddingInputKind.DOCUMENT
        )

        async with self._transactions.transaction() as connection:
            # The corpus lock, because `_resolve_path` below reads which paths
            # are taken and `vault_path` is UNIQUE -- the same race a
            # contribution has, for the same reason.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            run = await runs.get(connection, request.run_id)
            if run is not None and run.compiler_principal_id != request.principal_id:
                raise CompileRunNotYours(str(request.run_id))
            if run is None or run.state is not CompileRunState.RUNNING:
                # Re-checked under the lock: a finish may have landed while the
                # embedding call was in flight, and a page attributed to a
                # settled run would make its provenance a lie. `finish` and
                # `fail` take this same lock, which is what makes the check a
                # guard rather than a snapshot that can go stale a line later.
                raise CompileRunAlreadySettled(str(request.run_id))

            # Sources are re-validated here, not only before the embedding call.
            # That call is a third-party round trip taking seconds, and a
            # retirement can land inside it -- storing the page anyway would
            # write provenance naming a note that no longer exists, which is the
            # one thing `source_ids` validation exists to prevent. Checked under
            # the lock, which retirement also takes, so the answer cannot go
            # stale between here and the insert.
            notes = await wiki.note_states(connection)
            vanished = [
                source_id
                for source_id in request.source_ids
                if source_id not in notes
            ]
            if vanished:
                raise UnresolvedSources(vanished)

            if request.page_id is not None:
                # The target's kind is settled before anything is written.
                # `replace_content` matches on id alone, which is right for it
                # -- the ordinary update path edits notes through it -- so a
                # note id here would overwrite that note with page content and
                # discover the mistake afterwards, when the wiki-only
                # provenance update matched nothing. The transaction would roll
                # it back, but the caller would get a 500 for what is plainly a
                # bad field value.
                target = await documents.get_by_id(connection, request.page_id)
                if target is None:
                    raise DocumentNotFound(request.page_id)
                if target.kind is not DocumentKind.WIKI:
                    raise CompileTargetNotAPage(request.page_id, target.kind)
                stored = await documents.replace_content(
                    connection, request.page_id, candidate
                )
                if stored is None:
                    raise DocumentNotFound(request.page_id)
                # `replace_content` leaves compile provenance alone, because an
                # ordinary update must not claim to be a compilation. A
                # recompile genuinely is one, so it moves separately.
                stored = await documents.set_compile_provenance(
                    connection,
                    request.page_id,
                    compile_run_id=request.run_id,
                    compiled_by=f"agent:{request.principal_id}",
                    compiled_at=run.started_at,
                )
                if stored is None:
                    # Unreachable now the kind is checked above under this same
                    # lock. A raise rather than an `assert`, which -O strips --
                    # and the next line would then fail with an AttributeError
                    # on None, which tells a reader nothing.
                    raise DocumentNotFound(request.page_id)
            else:
                taken = await documents.vault_paths_under(
                    connection, AGENT_WIKI_DIRECTORY
                )
                candidate = replace(
                    candidate,
                    vault_path=resolve_vault_path(
                        AGENT_WIKI_DIRECTORY, candidate.title, taken
                    ),
                )
                stored = await documents.insert(connection, candidate)

            await embeddings.upsert(
                connection,
                DocumentEmbedding(
                    document_id=stored.id,
                    profile_id=self._provider.profile_id,
                    vector=vector,
                    text_sha256=digest,
                ),
            )
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.compile",
                outcome="page_written",
                request_id=request.request_id,
                principal_id=request.principal_id,
                target_type="document",
                target_id=stored.id,
            )
        return stored

    def _build_page(
        self,
        request: CompilePageRequest,
        run: VaultCompileRun,
    ) -> NewVaultDocument:
        """The row a compiled page becomes.

        ``compiled_at`` comes from the run's start rather than from ``now()``,
        so every page in one run carries one timestamp. A page-by-page clock
        would make "was this note newer than the page" depend on where in the
        run the page happened to be written.
        """

        return NewVaultDocument(
            id=request.page_id or uuid4().hex,
            kind=DocumentKind.WIKI,
            doc_type="Wiki Page",
            schema_version=WIKI_SCHEMA_VERSION,
            vault_path=resolve_vault_path(
                AGENT_WIKI_DIRECTORY, request.title, ()
            ),
            status=DocumentStatus.ACTIVE,
            doc_status="Current",
            title=request.title,
            summary=request.summary,
            body=request.body,
            tags=request.tags,
            related_ids=request.related_ids,
            source_ids=request.source_ids,
            contributed_by=f"agent:{request.principal_id}",
            provenance={"principal_id": request.principal_id},
            compile_run_id=run.id,
            compiled_by=f"agent:{request.principal_id}",
            compiled_at=run.started_at,
        )

    # -------------------------------------------------------------- decline --

    async def decline(
        self,
        run_id: UUID,
        principal_id: str,
        request_id: str,
        note_ids: Sequence[str],
    ) -> tuple[tuple[str, ...], datetime]:
        """Record that this run considered these notes and wants no page.

        The counterpart to ``write_page``, and the reason the plan can ever be
        empty. Without it the planner has to *infer* refusal from the passage of
        time, which is what the frontier did and what it could not do correctly:
        "I looked and decided against it" and "nobody ever showed me this" are
        different facts and only one of them should suppress a future offer.

        A separate call rather than a field on ``finish``, for the reason pages
        are separate: a run that declines and then fails keeps its declines, the
        same way it keeps its pages. Both are real judgements, and discarding
        them to reach a tidier state throws the work away.

        Declining a note the plan did not offer is allowed, deliberately -- the
        plan is advice (ADR 0027). Refusing it would mean a librarian who
        noticed something in passing had nowhere to put that.

        Ids that resolve to no live note are refused rather than ignored, the
        way a page's ``source_ids`` are: a decline naming nothing is a caller
        error, and silently dropping it would leave the note being offered
        forever with nothing to explain why.
        """

        runs = VaultCompileRunRepository()
        documents = VaultWikiPageRepository()
        declined_at = datetime.now(UTC)

        async with self._transactions.transaction() as connection:
            # The corpus lock, for the reason `finish` and `write_page` take it:
            # a judgement must not attach to a run that has already reported its
            # result, and the state check below is only a guard while the settle
            # path contends for the same lock.
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            run = await runs.get(connection, run_id)
            if run is None:
                raise CompileRunNotFound(str(run_id))
            if run.compiler_principal_id != principal_id:
                raise CompileRunNotYours(str(run_id))
            if run.state is not CompileRunState.RUNNING:
                raise CompileRunAlreadySettled(str(run_id))

            requested = tuple(dict.fromkeys(note_ids))
            reached = await documents.decline_notes(
                connection, requested, declined_at=declined_at
            )
            unresolved = [
                note_id for note_id in requested if note_id not in set(reached)
            ]
            if unresolved:
                raise UnresolvedSources(unresolved)

            audit = VaultAuditEventRepository()
            for note_id in reached:
                # One event per note, not one per call: each is a judgement
                # about a particular note, and `target_id` holds one id. This is
                # also where "who declined it, and when" lives -- the note keeps
                # only the timestamp, deliberately (see migration 0015).
                await audit.record(
                    connection,
                    operation="vault.compile",
                    outcome="declined",
                    request_id=request_id,
                    principal_id=principal_id,
                    target_type="document",
                    target_id=note_id,
                )

        return reached, declined_at

    # --------------------------------------------------------------- settle --

    async def finish(
        self,
        run_id: UUID,
        principal_id: str,
        request_id: str,
    ) -> VaultCompileRun:
        """Mark a run succeeded, publishing the frontier it was planned against.

        **The output frontier is the run's own ``input_frontier``, captured at
        plan time -- not a fresh maximum read here.** Reading it here loses
        notes permanently, which is the opposite of what an earlier version of
        this docstring claimed:

        1. a run is planned, capturing frontier F;
        2. a note lands afterwards, at T > F, so the plan never mentioned it and
           the compiler never saw it;
        3. the run finishes and a fresh maximum publishes T;
        4. the next incremental plan skips uncovered notes at or below T.

        That note is never offered again. Publishing F instead means the next
        plan re-considers everything written since planning began -- including
        work this run may already have covered, which is the harmless direction
        because a page that covers a note removes it from `new-source` anyway.

        The lock is the same one ``write_page`` takes. Without it that method's
        check that the run is still running is not a guard at all: it can pass,
        and this can settle and commit before the page insert lands, producing a
        page attributed to a run that has already reported its result.
        """

        runs = VaultCompileRunRepository()
        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            existing = await runs.get(connection, run_id)
            if existing is None:
                raise CompileRunNotFound(str(run_id))
            if existing.compiler_principal_id != principal_id:
                # A run settled by someone other than the principal who opened
                # it publishes a frontier on their behalf -- and the frontier is
                # what stops notes being re-offered, so settling another
                # principal's run silently narrows their next plan.
                raise CompileRunNotYours(str(run_id))
            settled = await runs.settle(
                connection,
                run_id,
                state=CompileRunState.SUCCEEDED,
                output_frontier=dict(existing.input_frontier),
            )
            if settled is None:
                raise await self._settle_failure(connection, runs, run_id)
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.compile",
                outcome="succeeded",
                request_id=request_id,
                principal_id=principal_id,
                target_type="compile_run",
                target_id=str(run_id),
            )
            return settled

    async def fail(
        self,
        run_id: UUID,
        principal_id: str,
        request_id: str,
        error_summary: str,
    ) -> VaultCompileRun:
        """Abandon a run, leaving whatever pages it wrote in place.

        Deliberately not a rollback. The pages a failed run committed are real
        synthesis and their provenance is accurate; discarding them would throw
        away work to reach a tidier state. What the failure changes is the
        frontier -- a failed run publishes none, so the next plan re-covers the
        notes this one did not finish.

        Takes the corpus lock for the reason ``finish`` does: a page write that
        has already checked the run state must not be able to commit after this
        settles.
        """

        runs = VaultCompileRunRepository()
        async with self._transactions.transaction() as connection:
            await connection.execute(
                text_sql("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _CONTRIBUTION_LOCK_KEY},
            )
            existing = await runs.get(connection, run_id)
            if existing is None:
                raise CompileRunNotFound(str(run_id))
            if existing.compiler_principal_id != principal_id:
                raise CompileRunNotYours(str(run_id))
            settled = await runs.settle(
                connection,
                run_id,
                state=CompileRunState.FAILED,
                error_summary=error_summary,
            )
            if settled is None:
                raise await self._settle_failure(connection, runs, run_id)
            await VaultAuditEventRepository().record(
                connection,
                operation="vault.compile",
                outcome="failed",
                request_id=request_id,
                principal_id=principal_id,
                target_type="compile_run",
                target_id=str(run_id),
            )
            return settled

    @staticmethod
    async def _settle_failure(
        connection: AsyncConnection,
        runs: "VaultCompileRunRepository",
        run_id: UUID,
    ) -> Exception:
        """Which of the two reasons a settle matched no row."""

        existing = await runs.get(connection, run_id)
        if existing is None:
            return CompileRunNotFound(str(run_id))
        return CompileRunAlreadySettled(str(run_id))


def _plan_items(
    *,
    pages: Sequence[VaultDocument],
    notes: Mapping[str, NoteCompileState],
    all_pages: bool,
) -> tuple[CompileWorkItem, ...]:
    """Which pages need writing, and why.

    Ported from the Stage-A engine's ``compute_stale`` and ``plan``, and kept
    diffable against them the way ``governance.py`` is kept diffable against
    ``vault_contrib.core`` (ADR 0004). Three reasons a page is stale, and they
    are not interchangeable:

    - **missing** -- it cites a note that no longer exists. The page makes a
      claim about provenance that is no longer true.
    - **stale** -- a source moved after the page was compiled, or has since
      been flagged. The synthesis is out of date rather than wrong.
    - **new-source** -- a note no page covers at all. Not a stale page; an
      absent one.

    Pure, and takes plain data rather than a connection, so the ordering rules
    can be tested without a database.
    """

    items: list[CompileWorkItem] = []
    covered: set[str] = set()

    for page in pages:
        covered.update(page.source_ids)
        page_sources = [sid for sid in page.source_ids if sid in notes]
        missing = len(page_sources) != len(page.source_ids)
        flagged = any(
            notes[sid].status != DocumentStatus.ACTIVE.value
            for sid in page_sources
        )
        moved = page.compiled_at is not None and any(
            notes[sid].updated_at > page.compiled_at for sid in page_sources
        )
        if all_pages or missing or flagged or moved:
            items.append(
                CompileWorkItem(
                    page_id=page.id,
                    title=page.title,
                    reason="missing" if missing else "stale",
                    source_ids=tuple(page.source_ids),
                )
            )

    for note_id, note in sorted(notes.items()):
        if note_id in covered or note.status != DocumentStatus.ACTIVE.value:
            continue
        if note.declined and not all_pages:
            # Considered and declined. Re-offering it every run would make the
            # plan permanently non-empty, which is the state in which nobody
            # reads it -- and that is the whole reason this check exists.
            #
            # It used to be a frontier comparison, and the difference is not
            # cosmetic. A frontier said "the corpus had moved this far", so a
            # note *excluded* from a plan was covered by it just the same as one
            # offered and refused: a flagged note advanced the frontier past
            # itself, and approving it later moved no timestamp, so it was never
            # offered again. Only notes somebody actually declined are marked
            # here, so a note nobody was shown stays offered.
            continue
        items.append(
            CompileWorkItem(
                page_id=None,
                title=None,
                reason="new-source",
                source_ids=(note_id,),
            )
        )

    return tuple(items)
