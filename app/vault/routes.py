"""Read-only HTTP surface for the vault.

Search and document retrieval only. Contribution, review, and compile endpoints
belong to later phases and are deliberately absent.

Access is gated on a single shared secret, ``VAULT_READ_API_KEY``. The vault
cannot reuse HighScoreServer's auth — importing it would breach the isolation
rule that keeps extraction a directory move — and a private corpus must not be
served to anonymous callers, so the minimum a read surface needs is implemented
here and nothing more. When the vault gains real agent credentials, this is the
seam they replace.
"""

import hmac
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .api_models import VaultDocumentDetail, VaultSearchHit, VaultSearchResponse
from .constants import resolve_text_search_config
from .db import get_vault_engine
from .domain import DocumentStatus, VaultDocument
from .embedding_runtime import get_embedding_provider
from .repository import VaultDocumentRepository
from .service import VaultSearchService, VaultTransactionService


logger = logging.getLogger(__name__)

router = APIRouter(tags=["vault"])

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
READABLE_STATUSES = (DocumentStatus.ACTIVE, DocumentStatus.ARCHIVED)


def _require_read_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Authorize a read request against the configured shared secret."""

    expected = os.environ.get("VAULT_READ_API_KEY", "")
    if not expected:
        # Refusing to serve is the safe failure. Serving an unauthenticated
        # corpus because a variable is missing is not.
        logger.error("VAULT_READ_API_KEY is not set; refusing vault read request")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault read access is not configured",
        )
    # Compared as bytes, not str: compare_digest raises TypeError on a str
    # holding non-ASCII, and the token is attacker-controlled — Starlette
    # decodes headers as latin-1, so a raw high byte reaches here and would
    # turn a failed authorization into a 500.
    provided = credentials.credentials.encode("utf-8") if credentials else b""
    if not hmac.compare_digest(provided, expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid vault credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
        status=document.status,
        title=document.title,
        summary=document.summary,
        body=document.body,
        tags=list(document.tags),
        related_ids=list(document.related_ids),
        source_ids=list(document.source_ids),
        source_url=document.source_url,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get(
    "/search",
    response_model=VaultSearchResponse,
    dependencies=[Depends(_require_read_key)],
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
    "/documents/{document_id}",
    response_model=VaultDocumentDetail,
    dependencies=[Depends(_require_read_key)],
    summary="Fetch one vault document by ID",
)
async def get_vault_document(
    document_id: str = Path(min_length=1, max_length=256),
) -> VaultDocumentDetail:
    transactions = VaultTransactionService(get_vault_engine())
    documents = VaultDocumentRepository()

    async with transactions.transaction() as connection:
        document = await documents.get_by_id(
            connection,
            document_id,
            statuses=READABLE_STATUSES,
        )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    return _to_detail(document)
