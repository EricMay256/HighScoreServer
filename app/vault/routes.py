"""HTTP surface for the vault.

Search, note retrieval, and the governed write path: contribution (ADR 0016) and
full replacement (ADR 0018), and retirement (ADR 0019). Review, compile, and
export endpoints belong to later phases and are deliberately absent — which means a *flagged* document can
be corrected through no surface here, as ADR 0018 records.

Access is gated on operator-issued agent credentials
(``hssv1_<credential-id>_<secret>``), verified against
``vault_agent_credentials``. The vault cannot reuse HighScoreServer's auth —
importing it would breach the isolation rule that keeps extraction a directory
move — and the integration spec is explicit that player JWTs and the global
leaderboard ``API_KEY`` are not vault credentials.
"""

import json
import logging
from hashlib import sha256
from uuid import uuid4

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
    VaultContributionRequest,
    VaultContributionResponse,
    VaultDocumentDetail,
    VaultDocumentUpdateRequest,
    VaultDocumentUpdateResponse,
    VaultSearchHit,
    VaultSearchResponse,
    VaultSimilarNote,
)
from .auth import VaultCredential, VaultScope, authorize, parse_token
from .constants import resolve_text_search_config
from .db import get_vault_engine
from .domain import VaultDocument
from .embedding_runtime import get_embedding_provider
from .embeddings import EmbeddingError, EmbeddingInputTooLong
from .rate_limit import enforce_preauth_ip_limit, get_limiter
from .read_policy import READABLE_STATUSES
from .repository import VaultAgentCredentialRepository, VaultDocumentRepository
from .service import (
    REQUEST_DIGEST_VERSION,
    ContributionRequest,
    DedupUnavailable,
    DocumentNotFound,
    DocumentUnderReview,
    IdempotencyConflict,
    RetireRequest,
    UpdateRequest,
    UpdateWouldDuplicate,
    VaultContributionService,
    VaultDocumentRetireService,
    VaultDocumentUpdateService,
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
    """Verify a bearer token and its scopes, or raise.

    401 and 403 are distinguished because they mean different things to an
    operator: a bad token is a client that cannot talk to us, a missing scope
    is a client we deliberately did not grant something. Neither response says
    which check failed.
    """

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid vault credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    parsed = parse_token(credentials.credentials) if credentials else None
    if parsed is None:
        raise unauthorized

    repository = VaultAgentCredentialRepository()
    transactions = VaultTransactionService(get_vault_engine())
    async with transactions.transaction() as connection:
        credential = await repository.get(connection, parsed.credential_id)
        failure = authorize(credential, parsed.secret, required_scopes)
        if failure is None and credential is not None:
            await repository.touch(connection, credential.id)

    if failure == "scope" and credential is not None:
        # Never log the token; the credential ID is the non-secret half and is
        # what an operator needs to find the row.
        logger.warning(
            "Vault credential lacks a required scope",
            extra={
                "credential_id": credential.id,
                "principal_id": credential.principal_id,
                "required_scopes": list(required_scopes),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential lacks the required scope",
        )
    if failure is not None:
        raise unauthorized
    if credential is None:  # unreachable; authorize() returns "invalid" first
        raise unauthorized
    return credential


async def _enforce_quota(credential: VaultCredential, operation: str) -> None:
    """Charge one request against the principal's quota for this operation."""

    retry_after = await get_limiter().check(credential.principal_id, operation)
    if retry_after is None:
        return
    logger.warning(
        "Vault rate limit exceeded",
        extra={
            "principal_id": credential.principal_id,
            "operation": operation,
            "retry_after": retry_after,
        },
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded",
        # Integer seconds: Retry-After has no fractional form, and rounding
        # down would invite a retry that is refused again.
        headers={"Retry-After": str(int(retry_after + 0.999))},
    )


async def require_read_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.READ,), credentials)


async def require_write_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    """Contribute only. See ADR 0020 for why this no longer covers all writes."""

    return await _authenticated((VaultScope.WRITE,), credentials)


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


def _search_service() -> VaultSearchService:
    return VaultSearchService(
        transactions=VaultTransactionService(get_vault_engine()),
        provider=get_embedding_provider(),
        text_search_config=resolve_text_search_config(),
    )


def _to_detail(document: VaultDocument) -> VaultDocumentDetail:
    return VaultDocumentDetail(
        note_id=document.id,
        kind=document.kind,
        doc_type=document.doc_type,
        vault_path=document.vault_path,
        status=document.status,
        doc_status=document.doc_status,
        title=document.title,
        summary=document.summary,
        body=document.body,
        tags=list(document.tags),
        aliases=list(document.aliases),
        facets={name: list(values) for name, values in document.facets.items()},
        related_ids=list(document.related_ids),
        source_ids=list(document.source_ids),
        source_url=document.source_url,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get(
    "/search",
    response_model=VaultSearchResponse,
    dependencies=[Depends(search_quota)],
    summary="Hybrid lexical and vector search over the vault corpus",
)
async def search_vault(
    q: str = Query(min_length=1, max_length=500, description="Search query."),
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
    return VaultSearchResponse(
        # The stripped query is what was actually searched, so it is what the
        # response reports.
        query=query,
        profile_id=outcome.profile_id,
        vector_status=outcome.vector_status,
        hits=[
            VaultSearchHit(
                **_to_detail(result.document).model_dump(),
                score=result.score,
                lexical_rank=result.lexical_rank,
                vector_rank=result.vector_rank,
            )
            for result in outcome.results
        ],
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
    return _to_detail(document)


async def write_quota(
    credential: VaultCredential = Depends(require_write_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "contribute")
    return credential


def _canonical_request_digest(body: VaultContributionRequest) -> bytes:
    """Hash the request so a reused idempotency key can be checked against it.

    Hashes the validated model rather than the raw bytes: two JSON documents
    differing only in key order or whitespace are the same request, and
    treating them as a conflict would refuse a legitimate retry.

    Only the fields the caller actually supplied are covered. Serializing unset
    fields at their defaults made the digest a function of the *server's* schema
    as well as of the request, so adding an optional field silently changed the
    digest of every request that had ever been made -- see migration 0006 and
    ADR 0016's amendment. ``exclude_unset`` keeps the key-order and whitespace
    property above while making additive schema change a non-event.

    Any change to this function is a new REQUEST_DIGEST_VERSION, because stored
    digests are not recomputable: the payloads that produced them were never
    kept.
    """

    canonical = json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).digest()


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
        request_sha256=_canonical_request_digest(body),
        digest_version=REQUEST_DIGEST_VERSION,
        request_id=request.headers.get("X-Request-Id") or uuid4().hex,
        tags=tuple(body.tags),
        summary=body.summary,
        aliases=tuple(body.aliases),
        facets=body.facets,
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

    return VaultContributionResponse(
        status=outcome.status,
        note_id=outcome.note_id,
        message=outcome.message,
        idempotent_replay=outcome.idempotent_replay,
        similars=[
            VaultSimilarNote(note_id=s.note_id, title=s.title, score=s.score)
            for s in outcome.similars
        ],
        errors=list(outcome.errors),
    )


async def update_quota(
    credential: VaultCredential = Depends(require_update_scope),
) -> VaultCredential:
    await _enforce_quota(credential, "update")
    return credential


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
