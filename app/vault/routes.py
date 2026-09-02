"""HTTP surface for the vault.

Search, listing, note retrieval, and the governed write path: contribution
(ADR 0016), full replacement (ADR 0018), retirement (ADR 0019), the summary
carveout (ADR 0035), amendment proposals (ADRs 0028, 0033, 0036), the review
queues (ADR 0019's amendment), and compilation (ADR 0027). Export has no
endpoint yet; ``vault:export`` is recognised and granted by no route. A
*flagged* document is served only through the review surface, as ADR 0008 and
ADR 0018 record.

Access is gated on operator-issued agent credentials
(``hssv1_<credential-id>_<secret>``), verified against
``vault_agent_credentials``. The vault cannot reuse HighScoreServer's auth —
importing it would breach the isolation rule that keeps extraction a directory
move — and the integration spec is explicit that player JWTs and the global
leaderboard ``API_KEY`` are not vault credentials.
"""

import logging
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .api_models import (
    VaultAmendmentBatchDecisionRequest,
    VaultAmendmentBatchDecisionResponse,
    VaultAmendmentBatchDecisionResult,
    VaultAmendmentDecisionRequest,
    VaultAmendmentDecisionResponse,
    VaultAmendmentProposalDetail,
    VaultAmendmentProposalRequest,
    VaultAmendmentProposalResponse,
    VaultAmendmentQueueResponse,
    VaultAuthorizationResponse,
    VaultCompileDeclineRequest,
    VaultCompileDeclineResponse,
    VaultCompilePageRequest,
    VaultCompilePlanResponse,
    VaultCompileRunSummary,
    VaultCompileSettleRequest,
    VaultContributionDetail,
    VaultContributionRequest,
    VaultContributionResponse,
    VaultDocumentDetail,
    VaultDocumentUpdateRequest,
    VaultDocumentUpdateResponse,
    VaultMetadataUpdateRequest,
    VaultNoteListResponse,
    VaultReviewCaseResponse,
    VaultReviewDecisionRequest,
    VaultReviewDecisionResponse,
    VaultReviewQueueResponse,
    VaultSearchResponse,
    VaultSetSummaryRequest,
    VaultSetSummaryResponse,
    amendment_metadata_preview,
    amendment_preview,
    amendment_proposal_change,
    amendment_proposal_summary,
    amendment_queue_previews,
    canonical_request_digest,
    compile_run_summary,
    compile_work_item,
    contribution_response,
    document_detail,
    note_summary,
    review_case_summary,
    search_response,
)
from .auth import VaultCredential, VaultScope
from .constants import SEARCH_QUERY_MAX_CHARS, resolve_text_search_config
from .db import get_vault_engine
from .domain import (
    AmendmentProposalKind,
    AmendmentProposalState,
    ReviewState,
    VaultCompileRun,
)
from .embedding_runtime import get_embedding_provider
from .embeddings import EmbeddingError, EmbeddingInputTooLong
from .facets import FACET_NAMES
from .principal import (
    VaultAuthError,
    VaultQuotaExceeded,
    VaultScopeError,
    charge_quota,
    resolve_credential,
)
from .rate_limit import enforce_preauth_ip_limit
from .read_policy import READABLE_PATH_PREFIXES, READABLE_STATUSES
from .repository import VaultDocumentRepository, VaultOAuthGrantRepository
from .service import (
    REQUEST_DIGEST_VERSION,
    AmendmentBaseRevisionMismatch,
    AmendmentDecisionRequest,
    AmendmentProposalAlreadyDecided,
    AmendmentProposalNotFound,
    AmendmentProposalRequest,
    AmendmentRemovalAcknowledgementRequired,
    CompilePageRequest,
    CompileRunAlreadySettled,
    CompileRunNotFound,
    CompileRunNotYours,
    CompileTargetNotAPage,
    ContributionRequest,
    DedupUnavailable,
    DocumentNotFound,
    DocumentUnderReview,
    IdempotencyConflict,
    MetadataChange,
    MetadataUpdateRequest,
    RetireRequest,
    ReviewCaseAlreadyDecided,
    ReviewCaseNotFound,
    ReviewDecisionRequest,
    SetSummaryRequest,
    SpanEdit,
    SummaryAlreadyPresent,
    SummaryRejected,
    SummaryStale,
    SummaryWindowClosed,
    UnresolvedSources,
    UpdateRequest,
    UpdateWouldDuplicate,
    VaultAmendmentService,
    VaultCompileService,
    VaultContributionService,
    VaultDocumentMetadataService,
    VaultDocumentRetireService,
    VaultDocumentSummaryService,
    VaultDocumentUpdateService,
    VaultReviewService,
    VaultSearchService,
    VaultTransactionService,
)


logger = logging.getLogger(__name__)

# The pre-auth guard is attached here rather than per route so it covers the
# whole surface, including routes added later, and so FastAPI solves it before
# the per-route dependency that authenticates -- which is the only ordering in
# which it protects anything. See rate_limit.enforce_preauth_ip_limit.
router = APIRouter(tags=["vault"], dependencies=[Depends(enforce_preauth_ip_limit)])

_bearer_scheme = HTTPBearer(auto_error=False)

# What the read surface will resolve by ID. Search returns active documents
# only; fetching by ID additionally resolves archived ones, because an archived
# document is retired but legitimate history and a related_ids or source_ids
# reference pointing at one should still resolve rather than dead-end.
#
# "flagged" is withheld. It means the write path's policy declined to endorse
# the content, and the vault's consumer is an agent, which is exactly the
# caller that will not think to check the status field before using what it
# was handed. Discovery and reference-resolution therefore differ by one
# status, deliberately, rather than by whichever predicate each query happened
# to carry. See vault ADR 0008, which also records why the restriction lives
# here rather than in the repository.
#
# Not configuration: a deployment must not be able to opt into serving
# unendorsed content.
#
# Defined in read_policy so the write path applies the same rule without
# importing this module. Imported above; the comment stays here because this is
# where a reader of the read surface looks for it.


# Advisory only, and deliberately equal to the default pool timeout: a caller
# that waited that long for a connection and lost has no better estimate to
# offer than "about as long as you just waited".
_SATURATION_RETRY_AFTER_SECONDS = 5


async def vault_saturation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map an exhausted vault connection pool to 503 rather than 500.

    SQLAlchemy raises ``TimeoutError`` when a checkout waits out
    ``pool_timeout`` — saturation, not a defect. Unhandled it becomes a 500,
    which is the wrong signal in two directions at once: the caller is told not
    to retry something that is purely transient, and the error tracker reports
    a bug where the truth is that the vault is busy.

    Registered by the host application because exception handlers live on the
    app, but written here so it leaves with the package. It names no HSS
    concept, and SQLAlchemy is the vault's dependency alone.
    """

    logger.warning(
        "Vault connection pool exhausted",
        extra={"path": request.url.path},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Vault is temporarily saturated"},
        headers={"Retry-After": str(_SATURATION_RETRY_AFTER_SECONDS)},
    )


async def _authenticated(
    required_scopes: tuple[str, ...],
    credentials: HTTPAuthorizationCredentials | None,
) -> VaultCredential:
    """Render ``principal.resolve_credential`` as HTTP.

    401 and 403 are distinguished because they mean different things to an
    operator: a bad token is a client that cannot talk to us, a missing scope
    is a client we deliberately did not grant something. Neither response says
    which check failed.

    The verification itself lives in ``principal`` so the MCP adapter can reach
    it without FastAPI's dependency system; this function is the HTTP rendering
    of the errors it raises and nothing more.
    """

    try:
        return await resolve_credential(
            credentials.credentials if credentials else None,
            required_scopes,
        )
    except VaultScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential lacks the required scope",
        ) from exc
    except VaultAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid vault credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def _enforce_quota(credential: VaultCredential, operation: str) -> None:
    """Charge one request against the principal's quota for this operation."""

    try:
        await charge_quota(credential, operation)
    except VaultQuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            # Integer seconds: Retry-After has no fractional form, and rounding
            # down would invite a retry that is refused again.
            headers={"Retry-After": str(int(exc.retry_after + 0.999))},
        ) from exc


async def require_read_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.READ,), credentials)


async def require_write_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    """Contribute only. See ADR 0020 for why this no longer covers all writes."""

    return await _authenticated((VaultScope.WRITE,), credentials)


async def require_propose_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.PROPOSE,), credentials)


async def require_update_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.UPDATE,), credentials)


async def require_delete_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.DELETE,), credentials)


async def search_quota(
    credential: VaultCredential = Depends(require_read_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "search")
    return credential


async def note_quota(
    credential: VaultCredential = Depends(require_read_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "get_note")
    return credential


async def authorization_quota(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    """Authenticate with no scope requirement, then charge the quota.

    Every other dependency here names a scope, and this one deliberately names
    none: the question it answers is "what is this credential", which the
    holder of the credential already knows. A scope would only decide which
    clients may be told about themselves, and refusing one would leave a
    console guessing at its own identity rather than reading it.
    """

    credential = await _authenticated((), credentials)
    await _enforce_quota(credential, "authorization")
    return credential


@router.get(
    "/authorization",
    response_model=VaultAuthorizationResponse,
    summary="Describe the credential presented on this request",
)
async def describe_authorization(
    credential: VaultCredential = Depends(authorization_quota),
) -> VaultAuthorizationResponse:
    """What this credential is, including its authorization's label.

    Exists for the consoles' headers. `oauth-<uuid4>` is exact and unreadable,
    and the label that fixes it is on the grant family, which a browser holding
    only an access token cannot see (ADR 0040). Everything else in the response
    is derivable from the token the caller already holds; the label is not.

    The label is unverified operator text on its way to a browser. It is
    returned as a JSON string and rendered through `textContent`, never as
    markup.
    """

    transactions = VaultTransactionService(get_vault_engine())
    grants = VaultOAuthGrantRepository()

    async with transactions.transaction() as connection:
        label = await grants.label_for_credential(connection, credential.id)

    return VaultAuthorizationResponse(
        credential_id=credential.id,
        principal_id=credential.principal_id,
        scopes=list(credential.scopes),
        label=label,
    )


def _search_service() -> VaultSearchService:
    return VaultSearchService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
        text_search_config=resolve_text_search_config(),
    )


@router.get(
    "/search",
    response_model=VaultSearchResponse,
    dependencies=[Depends(search_quota)],
    summary="Hybrid lexical and vector search over the vault corpus",
)
async def search_vault(
    q: str = Query(
        min_length=1,
        max_length=SEARCH_QUERY_MAX_CHARS,
        description="Search query.",
    ),
    limit: int = Query(default=10, ge=1, le=50),
) -> VaultSearchResponse:
    # min_length alone admits an all-whitespace query, which the embedding port
    # rejects with ValueError — not an EmbeddingError, so the service's
    # degradation path does not catch it and the request would 500. A query
    # with no searchable content is a bad request; refuse it at the boundary
    # where user input is validated.
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Search query must contain at least one non-whitespace character",
        )

    outcome = await _search_service().search(query, limit)
    return search_response(
        # The stripped query is what was actually searched, so it is what the
        # response reports.
        query=query,
        profile_id=outcome.profile_id,
        vector_status=outcome.vector_status,
        results=outcome.results,
        has_more=outcome.has_more,
    )


@router.get(
    "/notes/{note_id}",
    response_model=VaultDocumentDetail,
    dependencies=[Depends(note_quota)],
    summary="Fetch one vault note by ID",
)
async def get_vault_document(
    note_id: str = Path(min_length=1, max_length=256),
) -> VaultDocumentDetail:
    transactions = VaultTransactionService(get_vault_engine())
    documents = VaultDocumentRepository()

    async with transactions.transaction() as connection:
        document = await documents.get_by_id(
            connection,
            note_id,
            statuses=READABLE_STATUSES,
            readable_only=True,
        )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )
    return document_detail(document)


async def list_notes_quota(
    credential: VaultCredential = Depends(require_read_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "list_notes")
    return credential


# The bound on one page. Fifty rows of fixed-size fields is a few tens of
# kilobytes -- no bodies, no computed extracts -- so this needs no byte budget
# of the kind `search_response` carries. It bounds the query, not the prose.
MAX_NOTE_PAGE = 100
DEFAULT_NOTE_PAGE = 50


def _requested_facets(facet: list[str]) -> dict[str, list[str]]:
    """Parse `facet=name:value` pairs into the filter the repository takes.

    Repeating a name accumulates: `facet=project:hss&facet=project:b2` asks for
    a note in both, because every filter here narrows. A caller wanting either
    runs two requests -- an OR would need a syntax, and a listing that
    sometimes widens when you add a term is worse than one that cannot.

    Unknown names are refused rather than ignored. `FACET_NAMES` is closed
    (facets.py), so `projects=hss` is a typo, and answering it with an
    unfiltered page is answering a question nobody asked.

    Surrounding whitespace is incidental and is dropped. It has to be dropped
    *before* the checks rather than only inside them: validating `name.strip()`
    and then filtering on `name` is a parser that disagrees with itself, and it
    disagreed in both directions -- `facet= project:hss` was an unknown facet,
    and `facet=project: hss` filtered on a value no note can carry. Whitespace
    inside a value is left alone; a facet value may legitimately contain a
    space.
    """

    requested: dict[str, list[str]] = {}
    for pair in facet:
        raw_name, separator, raw_value = pair.partition(":")
        name, value = raw_name.strip(), raw_value.strip()
        if not separator or not name or not value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"facet must be 'name:value'; got {pair!r}",
            )
        if name not in FACET_NAMES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Unknown facet {name!r}. Known facets: "
                    f"{', '.join(sorted(FACET_NAMES))}"
                ),
            )
        requested.setdefault(name, []).append(value)
    return requested


@router.get(
    "/notes",
    response_model=VaultNoteListResponse,
    dependencies=[Depends(list_notes_quota)],
    summary="List notes by vault path, newest revision state, without bodies",
)
async def list_vault_documents(
    path: str | None = Query(
        default=None,
        max_length=1024,
        description=(
            "Restrict to one vault path prefix, for example "
            "'Human/03 Projects/'. Omitted lists everything the read policy "
            "allows. A prefix outside that policy is not an error and returns "
            "an empty page: what is readable is governance, not a permission "
            "this endpoint decides."
        ),
    ),
    tag: list[str] = Query(default=[], description="Every tag must be present."),
    facet: list[str] = Query(
        default=[],
        description=(
            "Facet filter as 'name:value', repeatable. Every one must match."
        ),
    ),
    after: str | None = Query(
        default=None,
        max_length=1024,
        description="The previous page's `next_cursor`, which is a vault_path.",
    ),
    limit: int = Query(default=DEFAULT_NOTE_PAGE, ge=1, le=MAX_NOTE_PAGE),
) -> VaultNoteListResponse:
    """Browse the corpus by where notes live rather than by what they match.

    `/search` ranks and `/notes/{id}` fetches; between them there was no way to
    *look around*, which is why reading the vault as a human meant exporting it
    to somewhere else first (ADR 0039).

    Ordered by `vault_path` and paged by keyset, because that is the corpus's
    own order: a listing sorted by relevance to no query would be arbitrary,
    and one sorted by time would scatter a folder across every page.

    The read policy is applied in the query, not to the page: filtering
    afterwards would return short pages and a cursor that skips whatever it
    dropped.
    """

    prefixes = (path,) if path is not None else READABLE_PATH_PREFIXES
    facets = _requested_facets(facet)

    transactions = VaultTransactionService(get_vault_engine())
    documents = VaultDocumentRepository()

    async with transactions.transaction() as connection:
        # One past the limit, so `has_more` is a fact rather than the guess a
        # full page would license. Briefs, not documents: this response
        # publishes no bodies, and reading them out of Postgres to drop them
        # here would make that a property of the projection rather than of the
        # query -- a hundred notes' worth of body per page, discarded.
        page = await documents.list_briefs_under_path_prefixes(
            connection,
            prefixes,
            after_vault_path=after,
            limit=limit + 1,
            statuses=READABLE_STATUSES,
            readable_only=True,
            tags=tag,
            facets=facets,
        )

    has_more = len(page) > limit
    visible = page[:limit]
    return VaultNoteListResponse(
        notes=[note_summary(document) for document in visible],
        has_more=has_more,
        next_cursor=visible[-1].vault_path if has_more and visible else None,
    )


async def write_quota(
    credential: VaultCredential = Depends(require_write_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "contribute")
    return credential


async def amendment_propose_quota(
    credential: VaultCredential = Depends(require_propose_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "amendment_propose")
    return credential


@router.post(
    "/contributions",
    response_model=VaultContributionResponse,
    status_code=status.HTTP_200_OK,
    summary="Contribute a note through the governed write path",
)
async def contribute(
    body: VaultContributionRequest,
    request: Request,
    credential: VaultCredential = Depends(write_quota),
) -> VaultContributionResponse:
    """Validate, embed, deduplicate, decide, and write.

    Returns 200 for every settled outcome including ``flagged`` and
    ``rejected``: the request was understood and processed, and the disposition
    is in the body. Reserving non-2xx for transport and authorization failures
    keeps a caller from treating "queued for review" as an error to retry.
    """

    service = VaultContributionService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
    )
    contribution = ContributionRequest(
        title=body.title,
        body=body.body,
        # The credential is the contributor. Taking it from the request body
        # would let one principal write under another's name.
        contributed_by=f"agent:{credential.principal_id}",
        principal_id=credential.principal_id,
        idempotency_key=body.idempotency_key,
        request_sha256=canonical_request_digest(body),
        digest_version=REQUEST_DIGEST_VERSION,
        request_id=request.headers.get("X-Request-Id") or uuid4().hex,
        tags=tuple(body.tags),
        summary=body.summary,
        aliases=tuple(body.aliases),
        facets=body.facets,
        origin=body.origin,
        related_ids=tuple(body.related_ids),
        source_ids=tuple(body.source_ids),
        source_url=str(body.source_url) if body.source_url else None,
    )

    try:
        outcome = await service.contribute(contribution)
    except IdempotencyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used for a different request",
        ) from exc
    except DedupUnavailable as exc:
        # Deliberately not a silent insert. Writing without the dedup gate
        # would defeat the guarantee the vault exists to provide.
        logger.error("Refusing a vault contribution: no embedding provider")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contribution is unavailable: no embedding provider configured",
        ) from exc
    except EmbeddingInputTooLong as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Document exceeds the embedding model input limit",
        ) from exc
    except EmbeddingError as exc:
        # Type only, never the message: an embedding exception can carry the
        # note body.
        logger.error(
            "Vault contribution failed to embed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contribution is temporarily unavailable",
        ) from exc

    if outcome.status == "invalid":
        # Governance validation, not transport validation: 422 per the spec.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": outcome.message, "errors": outcome.errors},
        )

    # Review detail, where the MCP surface defaults to outcome. The split is
    # deliberate and is about who is asking: this caller is a program, often
    # one building an adjudication surface, and its contract already carried
    # the gate's whole working. `max_similarity` is additive here -- nothing
    # was taken away. The MCP default is the narrow one because a model that
    # just wrote a note does not need ten scored note ids inviting a read.
    return contribution_response(
        outcome,
        detail=VaultContributionDetail.REVIEW,
        summary_supplied=body.summary is not None,
        # Unconditional: this handler runs under `vault:write`, which is the
        # scope the carveout runs under too, so the caller can act on it.
        summary_operation="POST /notes/{id}/summary",
    )


def _amendment_service() -> VaultAmendmentService:
    return VaultAmendmentService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
    )


@router.post(
    "/amendment-proposals",
    response_model=VaultAmendmentProposalResponse,
    status_code=status.HTTP_200_OK,
    summary="Propose a revision-bound change to an existing note",
)
async def propose_amendment(
    body: VaultAmendmentProposalRequest,
    request: Request,
    credential: VaultCredential = Depends(amendment_propose_quota),
) -> VaultAmendmentProposalResponse:
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    replacement = (
        body.change.replacement
        if body.change.kind == AmendmentProposalKind.REPLACEMENT.value
        else None
    )
    # A span is an authoring form, not a stored kind: the service resolves it
    # against the loaded body under the lock that checked `base_revision` and
    # writes the canonical diff, so it is labelled BODY_DIFF here and the
    # adapter does no converting of its own (ADR 0033). Same handling as the
    # MCP tool, deliberately -- the two surfaces must not disagree about what
    # a span becomes.
    is_span = body.change.kind == "span"
    proposal_request = AmendmentProposalRequest(
        target_document_id=body.target_note_id,
        base_revision=body.base_revision,
        change_kind=(
            AmendmentProposalKind.BODY_DIFF
            if is_span
            else AmendmentProposalKind(body.change.kind)
        ),
        rationale=body.rationale,
        principal_id=credential.principal_id,
        request_id=request_id,
        replacement=(
            UpdateRequest(
                document_id=body.target_note_id,
                title=replacement.title,
                body=replacement.body,
                principal_id=credential.principal_id,
                request_id=request_id,
                summary=replacement.summary,
                tags=tuple(replacement.tags),
                aliases=tuple(replacement.aliases),
                facets=replacement.facets,
                related_ids=tuple(replacement.related_ids),
                source_ids=tuple(replacement.source_ids),
                source_url=(
                    str(replacement.source_url) if replacement.source_url else None
                ),
            )
            if replacement is not None
            else None
        ),
        body_diff=(
            body.change.body_diff
            if body.change.kind == AmendmentProposalKind.BODY_DIFF.value
            else None
        ),
        span=(
            SpanEdit(
                expected_text=body.change.expected_text,
                replacement_text=body.change.replacement_text,
                occurrence=body.change.occurrence,
            )
            if is_span
            else None
        ),
        metadata=(
            MetadataChange(
                related_ids=(
                    tuple(body.change.related_ids)
                    if body.change.related_ids is not None
                    else None
                ),
                source_ids=(
                    tuple(body.change.source_ids)
                    if body.change.source_ids is not None
                    else None
                ),
                facets=body.change.facets,
                source_url=(
                    str(body.change.source_url) if body.change.source_url else None
                ),
                clear_source_url=body.change.clear_source_url,
            )
            if body.change.kind == AmendmentProposalKind.METADATA.value
            else None
        ),
    )
    try:
        proposal = await _amendment_service().propose(proposal_request)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    except AmendmentBaseRevisionMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The note changed; fetch it again before proposing an amendment",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return VaultAmendmentProposalResponse(
        proposal=amendment_proposal_summary(proposal)
    )


async def update_quota(
    credential: VaultCredential = Depends(require_update_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "update")
    return credential


@router.patch(
    "/notes/{note_id}/metadata",
    response_model=VaultDocumentUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Change one note's edges and classification",
)
async def update_vault_document_metadata(
    body: VaultMetadataUpdateRequest,
    request: Request,
    note_id: str = Path(min_length=1, max_length=256),
    credential: VaultCredential = Depends(update_quota),
) -> VaultDocumentUpdateResponse:
    """Change a note's edges or classification without resending it.

    PATCH rather than PUT because it genuinely is partial: the fields it does
    not name keep their stored values, which is the whole point. The full
    replacement at `PUT /notes/{note_id}` remains the path for content.

    `base_revision` is required and checked. A note that moved since the caller
    read it produces 409 rather than an overwrite.

    Costs no embedding call and runs no dedup gate, because nothing this
    accepts joins the embedding text -- so the note's vector still describes it
    and it cannot have become a duplicate of anything (ADR 0036).
    """

    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    change = MetadataChange(
        related_ids=(
            tuple(body.related_ids) if body.related_ids is not None else None
        ),
        source_ids=(
            tuple(body.source_ids) if body.source_ids is not None else None
        ),
        facets=body.facets,
        source_url=str(body.source_url) if body.source_url else None,
        clear_source_url=body.clear_source_url,
    )
    if change.is_empty():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Nothing to change. Provide at least one of related_ids, "
                "source_ids, facets, source_url, or clear_source_url."
            ),
        )

    service = VaultDocumentMetadataService(
        VaultTransactionService(get_vault_engine())
    )
    try:
        updated = await service.update(
            MetadataUpdateRequest(
                document_id=note_id,
                base_revision=body.base_revision,
                change=change,
                principal_id=credential.principal_id,
                request_id=request_id,
            )
        )
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        ) from exc
    except AmendmentBaseRevisionMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "retryable": True},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Invalid metadata change", "errors": [str(exc)]},
        ) from exc

    return VaultDocumentUpdateResponse(
        note_id=updated.id,
        message="metadata updated",
        # Always false, and worth stating rather than defaulting: nothing this
        # path accepts joins the embedding text, so the note's vector still
        # describes it and no provider call was spent (ADR 0036).
        re_embedded=False,
    )


@router.put(
    "/notes/{note_id}",
    response_model=VaultDocumentUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Replace one vault note's content",
)
async def update_vault_document(
    body: VaultDocumentUpdateRequest,
    request: Request,
    note_id: str = Path(min_length=1, max_length=256),
    credential: VaultCredential = Depends(update_quota),
) -> VaultDocumentUpdateResponse:
    """Replace a document's caller-supplied content.

    A replacement rather than a patch, and idempotent for that reason: the body
    states what the document should now be, so sending it twice converges. No
    idempotency key, because there is no identity to mint twice.

    Runs the same dedup gate a contribution does, excluding the document being
    updated. A collision is 409 and writes nothing -- see ADR 0018 for why an
    update refuses where a contribution flags.
    """

    service = VaultDocumentUpdateService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
    )
    update = UpdateRequest(
        document_id=note_id,
        title=body.title,
        body=body.body,
        principal_id=credential.principal_id,
        request_id=request.headers.get("X-Request-Id") or uuid4().hex,
        summary=body.summary,
        tags=tuple(body.tags),
        aliases=tuple(body.aliases),
        facets=body.facets,
        related_ids=tuple(body.related_ids),
        source_ids=tuple(body.source_ids),
        source_url=str(body.source_url) if body.source_url else None,
    )

    try:
        outcome = await service.update(update)
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        ) from exc
    except UpdateWouldDuplicate as exc:
        # 409 rather than 422: the replacement is well-formed, it just collides
        # with a document that already exists. Nothing was written.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "similars": [
                    {"note_id": s.note_id, "title": s.title, "score": s.score}
                    for s in exc.similars
                ],
            },
        ) from exc
    except DedupUnavailable as exc:
        logger.error("Refusing a vault update: no embedding provider")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Update is unavailable: no embedding provider configured",
        ) from exc
    except EmbeddingInputTooLong as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Document exceeds the embedding model input limit",
        ) from exc
    except EmbeddingError as exc:
        # Type only, never the message: an embedding exception can carry the
        # note body.
        logger.error(
            "Vault update failed to embed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Update is temporarily unavailable",
        ) from exc

    if outcome.errors:
        # Governance validation, not transport validation: 422 per the spec.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": outcome.message, "errors": list(outcome.errors)},
        )

    return VaultDocumentUpdateResponse(
        note_id=outcome.note_id,
        message=outcome.message,
        re_embedded=outcome.re_embedded,
    )


async def set_summary_quota(
    credential: VaultCredential = Depends(require_write_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "set_summary")
    return credential


@router.post(
    "/notes/{note_id}/summary",
    response_model=VaultSetSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Supply a summary a contribution omitted",
)
async def set_vault_document_summary(
    body: VaultSetSummaryRequest,
    request: Request,
    note_id: str = Path(min_length=1, max_length=256),
    credential: VaultCredential = Depends(set_summary_quota),
) -> VaultSetSummaryResponse:
    """Fill in an absent summary on the caller's own recent note (ADR 0035).

    Under `vault:write` rather than `vault:update`, which is the whole point:
    the contributor that omitted a summary can supply one without holding
    replacement authority over the corpus. It is a sub-resource rather than a
    PATCH on the note for the same reason -- a partial update of `/notes/{id}`
    would imply the other fields are reachable here, and they are not.

    Refuses three ways, each distinct in the status it returns: 404 when the
    note is not the caller's own (or does not exist -- see the service on why
    those are one answer), 409 when it already has a summary or the grace
    period has closed, and 409 again when the resulting note would duplicate a
    different one.
    """

    service = VaultDocumentSummaryService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
    )

    try:
        outcome = await service.set_summary(
            SetSummaryRequest(
                document_id=note_id,
                summary=body.summary,
                principal_id=credential.principal_id,
                # From the credential, never the body. Same claim and same
                # reasoning as the contribution path's.
                contributed_by=f"agent:{credential.principal_id}",
                request_id=request.headers.get("X-Request-Id") or uuid4().hex,
            )
        )
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        ) from exc
    except SummaryAlreadyPresent as exc:
        # 409 rather than 422: the request is well-formed and the caller is
        # entitled to make it; the resource is simply not in a state that
        # accepts it. Same reading as the update path's collision.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except SummaryWindowClosed as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "grace_seconds": exc.grace_seconds,
            },
        ) from exc
    except UpdateWouldDuplicate as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "similars": [
                    {"note_id": s.note_id, "title": s.title, "score": s.score}
                    for s in exc.similars
                ],
            },
        ) from exc
    except SummaryStale as exc:
        # 409 like the others, but this one is worth retrying: the note moved
        # while the summary was being embedded, and a second attempt embeds
        # against the current text.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "retryable": True},
        ) from exc
    except SummaryRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Summary failed validation", "errors": [str(exc)]},
        ) from exc
    except DedupUnavailable as exc:
        logger.error("Refusing a vault summary: no embedding provider")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Setting a summary is unavailable: no embedding provider configured",
        ) from exc
    except EmbeddingInputTooLong as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Document exceeds the embedding model input limit",
        ) from exc
    except EmbeddingError as exc:
        # Type only, never the message: an embedding exception can carry the
        # note body.
        logger.error(
            "Vault summary failed to embed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Setting a summary is temporarily unavailable",
        ) from exc

    return VaultSetSummaryResponse(
        note_id=outcome.note_id,
        message=outcome.message,
        content_revision=outcome.content_revision,
    )


async def retire_quota(
    credential: VaultCredential = Depends(require_delete_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "retire")
    return credential


@router.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove one vault note",
)
async def retire_vault_document(
    request: Request,
    note_id: str = Path(min_length=1, max_length=256),
    credential: VaultCredential = Depends(retire_quota),
) -> Response:
    """Remove a document from the vault.

    Deletion, not an archived status. ADR 0008's archived state is right for
    content that is superseded but true; this exists for content that is
    *wrong*, where a row a caller can still resolve by id is the failure rather
    than the record. See ADR 0019.

    204 with no body: there is nothing meaningful to return about a document
    that no longer exists, and repeating the id back would suggest otherwise.
    """

    service = VaultDocumentRetireService(
        transactions=VaultTransactionService(get_vault_engine()),
    )
    try:
        await service.retire(
            RetireRequest(
                document_id=note_id,
                principal_id=credential.principal_id,
                request_id=request.headers.get("X-Request-Id") or uuid4().hex,
            )
        )
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        ) from exc
    except DocumentUnderReview as exc:
        # 409 rather than 403: the request is legitimate and may succeed later,
        # once the review case is settled. Nothing was deleted.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot retire a document under review: {exc}",
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def require_review_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.REVIEW,), credentials)


async def review_list_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "review_list")
    return credential


async def review_read_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "review_read")
    return credential


async def review_decide_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "review_decide")
    return credential


async def amendment_list_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "amendment_list")
    return credential


async def amendment_read_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "amendment_read")
    return credential


async def amendment_decide_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "amendment_decide")
    return credential


async def amendment_batch_decide_quota(
    credential: VaultCredential = Depends(require_review_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "amendment_decide_batch")
    return credential


def _review_service() -> VaultReviewService:
    return VaultReviewService(VaultTransactionService(get_vault_engine()))


@router.get(
    "/reviews",
    response_model=VaultReviewQueueResponse,
    dependencies=[Depends(review_list_quota)],
    summary="List near-duplicate cases awaiting a decision",
)
async def list_review_queue(
    limit: int = Query(default=50, ge=1, le=200),
) -> VaultReviewQueueResponse:
    """The unresolved review backlog, oldest first.

    Oldest first because this is a backlog rather than a feed: the case most at
    risk of being forgotten is the one that has waited longest.
    """

    cases = await _review_service().list_pending(limit)
    return VaultReviewQueueResponse(
        pending=[review_case_summary(case) for case in cases],
        count=len(cases),
    )


@router.get(
    "/reviews/{review_case_id}",
    response_model=VaultReviewCaseResponse,
    dependencies=[Depends(review_read_quota)],
    summary="Read one review case and the note it concerns",
)
async def read_review_case(review_case_id: UUID) -> VaultReviewCaseResponse:
    """One case, with the flagged note in full.

    This is the only surface that serves ``flagged`` content. ADR 0008 withholds
    it everywhere else because the consumer there is a model that will not check
    the status field; a reviewer is the opposite consumer, and cannot adjudicate
    a note they cannot read. That is also why the whole review surface is gated
    on its own scope and stays off the MCP tool list (ADR 0021).
    """

    try:
        case, candidate = await _review_service().get(review_case_id)
    except ReviewCaseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review case not found",
        ) from exc

    return VaultReviewCaseResponse(
        review_case=review_case_summary(case),
        candidate=document_detail(candidate) if candidate is not None else None,
    )


@router.post(
    "/reviews/{review_case_id}/decision",
    response_model=VaultReviewDecisionResponse,
    dependencies=[Depends(review_decide_quota)],
    summary="Settle one review case",
)
async def decide_review_case(
    request: Request,
    body: VaultReviewDecisionRequest,
    review_case_id: UUID,
    credential: VaultCredential = Depends(review_decide_quota),
) -> VaultReviewDecisionResponse:
    """Accept or reject a flagged contribution.

    ``accepted`` publishes the note. ``rejected`` **deletes** it: a review
    candidate is always a brand-new note, so a duplicate judged redundant at
    birth has no history to preserve and its content is already in the corpus.
    ADR 0019 archives what is overtaken and deletes what is wrong.

    The decision is recorded either way. A rejected case survives with a null
    candidate pointer rather than vanishing with the note.
    """

    try:
        outcome = await _review_service().decide(
            ReviewDecisionRequest(
                review_case_id=review_case_id,
                state=ReviewState(body.decision),
                principal_id=credential.principal_id,
                request_id=request.headers.get("X-Request-Id") or uuid4().hex,
                decision_note=body.decision_note,
            )
        )
    except ReviewCaseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review case not found",
        ) from exc
    except ReviewCaseAlreadyDecided as exc:
        # 409 rather than 404: the case exists and someone else settled it.
        # Nothing was changed by this request, and the caller should re-read
        # rather than retry.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Review case has already been decided",
        ) from exc

    return VaultReviewDecisionResponse(
        review_case=review_case_summary(outcome.review_case),
        candidate=outcome.candidate,
    )


@router.get(
    "/amendment-proposals",
    response_model=VaultAmendmentQueueResponse,
    dependencies=[Depends(amendment_list_quota)],
    summary="List amendment proposals awaiting review",
)
async def list_amendment_proposals(
    limit: int = Query(default=50, ge=1, le=200),
) -> VaultAmendmentQueueResponse:
    proposals, previews, titles = await _amendment_service().list_pending_previews(
        limit
    )
    rendered, truncated = amendment_queue_previews(proposals, previews, titles)
    return VaultAmendmentQueueResponse(
        pending=[amendment_proposal_summary(item) for item in proposals],
        count=len(proposals),
        previews=rendered,
        truncated=truncated,
    )


@router.get(
    "/amendment-proposals/{proposal_id}",
    response_model=VaultAmendmentProposalDetail,
    dependencies=[Depends(amendment_read_quota)],
    summary="Read one amendment proposal and its current target",
)
async def read_amendment_proposal(proposal_id: UUID) -> VaultAmendmentProposalDetail:
    service = _amendment_service()
    try:
        proposal, target = await service.get(proposal_id)
    except AmendmentProposalNotFound as exc:
        raise HTTPException(status_code=404, detail="Amendment proposal not found") from exc
    return VaultAmendmentProposalDetail(
        proposal=amendment_proposal_summary(proposal),
        change=amendment_proposal_change(proposal),
        target=document_detail(target) if target is not None else None,
        preview=amendment_preview(service.preview(proposal, target)),
        metadata_preview=amendment_metadata_preview(
            await service.metadata_preview(proposal, target)
        ),
    )


def _amendment_decision_error(exc: Exception) -> HTTPException:
    """Map one service refusal onto the shared HTTP contract."""

    if isinstance(exc, AmendmentProposalNotFound):
        return HTTPException(status_code=404, detail="Amendment proposal not found")
    if isinstance(exc, AmendmentProposalAlreadyDecided):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Amendment proposal has already been decided",
        )
    if isinstance(exc, AmendmentRemovalAcknowledgementRequired):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, UpdateWouldDuplicate):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "similars": [
                    {"note_id": item.note_id, "title": item.title, "score": item.score}
                    for item in exc.similars
                ],
            },
        )
    if isinstance(exc, DedupUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Amendment acceptance is unavailable: no embedding provider configured",
        )
    if isinstance(exc, EmbeddingInputTooLong):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Document exceeds the embedding model input limit",
        )
    if isinstance(exc, EmbeddingError):
        logger.error(
            "Vault amendment failed to embed",
            extra={"error_type": type(exc).__name__},
        )
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Amendment acceptance is temporarily unavailable",
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )
    raise TypeError(f"Unhandled amendment decision error: {type(exc).__name__}")


_AMENDMENT_DECISION_ERRORS = (
    AmendmentProposalNotFound,
    AmendmentProposalAlreadyDecided,
    AmendmentRemovalAcknowledgementRequired,
    UpdateWouldDuplicate,
    DedupUnavailable,
    EmbeddingInputTooLong,
    EmbeddingError,
    ValueError,
)


@router.post(
    "/amendment-proposals/batch-decisions",
    response_model=VaultAmendmentBatchDecisionResponse,
    summary="Accept or reject a bounded batch of amendment proposals",
)
async def decide_amendment_proposal_batch(
    body: VaultAmendmentBatchDecisionRequest,
    request: Request,
    credential: VaultCredential = Depends(amendment_batch_decide_quota),
) -> VaultAmendmentBatchDecisionResponse:
    """Settle up to fifty proposals without one quota charge per card.

    Items are independent. Each decision keeps its own transaction and audit
    event, so a stale, invalid, or concurrently settled proposal is reported
    beside successful decisions instead of rolling the whole operator action
    back. The compact response omits target bodies; the console already has the
    reviewed previews and only needs each outcome.
    """

    service = _amendment_service()
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    results: list[VaultAmendmentBatchDecisionResult] = []

    requests = tuple(
        AmendmentDecisionRequest(
            proposal_id=item.proposal_id,
            state=AmendmentProposalState(item.decision),
            principal_id=credential.principal_id,
            request_id=request_id,
            decision_note=item.decision_note,
            acknowledge_removals=item.acknowledge_removals,
        )
        for item in body.decisions
    )
    for item, outcome in zip(
        body.decisions, await service.decide_batch(requests), strict=True
    ):
        if isinstance(outcome, _AMENDMENT_DECISION_ERRORS):
            problem = _amendment_decision_error(outcome)
            results.append(
                VaultAmendmentBatchDecisionResult(
                    proposal_id=item.proposal_id,
                    status_code=problem.status_code,
                    detail=problem.detail,
                )
            )
        elif isinstance(outcome, Exception):
            logger.error(
                "Unexpected vault batch amendment decision failure",
                extra={"error_type": type(outcome).__name__},
            )
            results.append(
                VaultAmendmentBatchDecisionResult(
                    proposal_id=item.proposal_id,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Amendment decision failed unexpectedly",
                )
            )
        else:
            results.append(
                VaultAmendmentBatchDecisionResult(
                    proposal_id=item.proposal_id,
                    outcome=outcome.outcome,
                    status_code=status.HTTP_200_OK,
                )
            )

    decided = sum(item.outcome is not None for item in results)
    return VaultAmendmentBatchDecisionResponse(
        results=results,
        decided=decided,
        refused=len(results) - decided,
    )


@router.post(
    "/amendment-proposals/{proposal_id}/decision",
    response_model=VaultAmendmentDecisionResponse,
    summary="Accept or reject one amendment proposal",
)
async def decide_amendment_proposal(
    proposal_id: UUID,
    body: VaultAmendmentDecisionRequest,
    request: Request,
    credential: VaultCredential = Depends(amendment_decide_quota),
) -> VaultAmendmentDecisionResponse:
    try:
        outcome = await _amendment_service().decide(
            AmendmentDecisionRequest(
                proposal_id=proposal_id,
                state=AmendmentProposalState(body.decision),
                principal_id=credential.principal_id,
                request_id=request.headers.get("X-Request-Id") or uuid4().hex,
                decision_note=body.decision_note,
                acknowledge_removals=body.acknowledge_removals,
            )
        )
    except _AMENDMENT_DECISION_ERRORS as exc:
        raise _amendment_decision_error(exc) from exc

    return VaultAmendmentDecisionResponse(
        proposal=amendment_proposal_summary(outcome.proposal),
        outcome=outcome.outcome,
        target=document_detail(outcome.target) if outcome.target is not None else None,
    )


async def require_compile_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.COMPILE,), credentials)


async def compile_plan_quota(
    credential: VaultCredential = Depends(require_compile_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "compile_plan")
    return credential


async def compile_write_quota(
    credential: VaultCredential = Depends(require_compile_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "compile_write")
    return credential


async def compile_settle_quota(
    credential: VaultCredential = Depends(require_compile_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "compile_settle")
    return credential


def _compile_service() -> VaultCompileService:
    return VaultCompileService(
        VaultTransactionService(get_vault_engine()),
        get_embedding_provider(),
    )


@router.post(
    "/compile/runs",
    response_model=VaultCompilePlanResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(compile_plan_quota)],
    summary="Open a compile run and plan the pages it should write",
)
async def plan_compile_run(
    all_pages: bool = Query(
        default=False,
        description=(
            "Force a full recompile. The default is incremental: only pages "
            "whose sources moved, and only notes written since the last "
            "successful run's frontier."
        ),
    ),
    credential: VaultCredential = Depends(compile_plan_quota),
) -> VaultCompilePlanResponse:
    """Open a run and describe the work.

    The service plans; the agent synthesizes. Which pages are stale is a query;
    writing the prose that distils four notes into one page is not, which is
    why this returns work items rather than pages.

    Opening a run has a cost worth knowing: the row is created `running` and
    stays that way until finished or failed, and only a successful run
    publishes a frontier. A loop that plans without settling accumulates runs
    nobody closed, which is why the quota here is the tightest of the three.
    """

    plan = await _compile_service().plan(
        credential.principal_id, all_pages=all_pages
    )
    return VaultCompilePlanResponse(
        run=compile_run_summary(plan.run),
        items=[compile_work_item(item) for item in plan.items],
        count=len(plan.items),
    )


@router.post(
    "/compile/runs/{run_id}/pages",
    response_model=VaultDocumentDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(compile_write_quota)],
    summary="Write one compiled wiki page into an open run",
)
async def write_compile_page(
    request: Request,
    body: VaultCompilePageRequest,
    run_id: UUID,
    credential: VaultCredential = Depends(compile_write_quota),
) -> VaultDocumentDetail:
    """Store one page, embedded and attributed to this run.

    No dedup gate: a compiled page restates its sources by construction, so
    scoring it against them would flag every page ever written. Pages are still
    embedded, because search must be able to return synthesis.

    `source_ids` are validated and refused when unresolved. That is the
    opposite of a note's `related_ids`, which ADR 0025 keeps opaque on purpose
    -- provenance naming something that never existed is a false claim rather
    than a dangling edge.
    """

    try:
        page = await _compile_service().write_page(
            CompilePageRequest(
                run_id=run_id,
                title=body.title,
                body=body.body,
                source_ids=tuple(body.source_ids),
                principal_id=credential.principal_id,
                request_id=request.headers.get("X-Request-Id") or uuid4().hex,
                summary=body.summary,
                tags=tuple(body.tags),
                related_ids=tuple(body.related_ids),
                page_id=body.page_id,
            )
        )
    except CompileRunNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Compile run not found",
        ) from exc
    except CompileRunNotYours as exc:
        # 409 and not 403: the scope already permits writing wiki pages, and the
        # caller may open its own run whenever it likes. What it may not do is
        # attribute work to someone else's run -- a consistency rule about this
        # run, not a permission the caller lacks.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run belongs to a different principal",
        ) from exc
    except CompileRunAlreadySettled as exc:
        # 409 rather than 404: the run exists, and the caller can open a new
        # one. A page attributed to a settled run would make its provenance a
        # lie, which is why this is refused rather than accepted late.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run is already settled; open a new run",
        ) from exc
    except UnresolvedSources as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except CompileTargetNotAPage as exc:
        # 422 and not 404: the document exists, so "not found" would send the
        # caller looking for a missing row instead of at the id it sent. Same
        # status as UnresolvedSources, and for the same reason -- a field in the
        # body names the wrong kind of thing.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Page not found",
        ) from exc
    except DedupUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return document_detail(page)


@router.post(
    "/compile/runs/{run_id}/declines",
    response_model=VaultCompileDeclineResponse,
    dependencies=[Depends(compile_write_quota)],
    summary="Record notes this run considered and will not compile",
)
async def decline_compile_notes(
    request: Request,
    body: VaultCompileDeclineRequest,
    run_id: UUID,
    credential: VaultCredential = Depends(compile_write_quota),
) -> VaultCompileDeclineResponse:
    """Say so, rather than leaving the planner to infer it from a timestamp.

    This is what lets the plan be empty without the service having to guess. A
    note nobody declined keeps being offered; a note declined here stops, until
    it changes.

    On the write quota rather than the settle one: it is a mutation the run
    makes repeatedly, like writing a page, not the single act of closing out.
    """

    service = _compile_service()
    try:
        declined, declined_at = await service.decline(
            run_id,
            credential.principal_id,
            request.headers.get("X-Request-Id") or uuid4().hex,
            body.note_ids,
        )
    except CompileRunNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Compile run not found",
        ) from exc
    except CompileRunNotYours as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run belongs to a different principal",
        ) from exc
    except CompileRunAlreadySettled as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run is already settled; open a new run",
        ) from exc
    except UnresolvedSources as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return VaultCompileDeclineResponse(
        declined_note_ids=list(declined),
        declined_at=declined_at,
    )


@router.post(
    "/compile/runs/{run_id}/finish",
    response_model=VaultCompileRunSummary,
    dependencies=[Depends(compile_settle_quota)],
    summary="Mark a compile run succeeded and publish its frontier",
)
async def finish_compile_run(
    request: Request,
    run_id: UUID,
    credential: VaultCredential = Depends(compile_settle_quota),
) -> VaultCompileRunSummary:
    """Settle a run, publishing the frontier it was planned against.

    Not a frontier read here: that would count a note written *after* planning
    as covered, when the plan never mentioned it and the compiler never saw it,
    and no later plan would offer it either. Publishing the plan-time frontier
    may re-offer something this run already handled, which is harmless — a page
    covering a note removes it from `new-source` anyway.
    """

    return compile_run_summary(
        await _settle(
            run_id,
            credential,
            request,
            fail_with=None,
        )
    )


@router.post(
    "/compile/runs/{run_id}/fail",
    response_model=VaultCompileRunSummary,
    dependencies=[Depends(compile_settle_quota)],
    summary="Abandon a compile run, keeping the pages it wrote",
)
async def fail_compile_run(
    request: Request,
    body: VaultCompileSettleRequest,
    run_id: UUID,
    credential: VaultCredential = Depends(compile_settle_quota),
) -> VaultCompileRunSummary:
    """Abandon a run without discarding its work.

    Deliberately not a rollback. Pages a failed run committed are real
    synthesis and their provenance is accurate; throwing them away to reach a
    tidier state would lose work. What the failure changes is the frontier -- a
    failed run publishes none, so the next plan re-covers what this one left.
    """

    if not (body.error_summary or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="error_summary is required when failing a run",
        )
    return compile_run_summary(
        await _settle(run_id, credential, request, fail_with=body.error_summary)
    )


async def _settle(
    run_id: UUID,
    credential: VaultCredential,
    request: Request,
    *,
    fail_with: str | None,
) -> VaultCompileRun:
    """Shared error rendering for the two settle verbs."""

    service = _compile_service()
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    try:
        if fail_with is None:
            return await service.finish(run_id, credential.principal_id, request_id)
        return await service.fail(
            run_id, credential.principal_id, request_id, fail_with
        )
    except CompileRunNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Compile run not found",
        ) from exc
    except CompileRunNotYours as exc:
        # 409 and not 403: the scope already permits writing wiki pages, and the
        # caller may open its own run whenever it likes. What it may not do is
        # attribute work to someone else's run -- a consistency rule about this
        # run, not a permission the caller lacks.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run belongs to a different principal",
        ) from exc
    except CompileRunAlreadySettled as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compile run is already settled",
        ) from exc
