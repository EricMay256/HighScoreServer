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
from .domain import VaultDocument
from .embedding_runtime import get_embedding_provider
from .repository import VaultDocumentRepository
from .service import VaultSearchService, VaultTransactionService


logger = logging.getLogger(__name__)

router = APIRouter(tags=["vault"])

_bearer_scheme = HTTPBearer(auto_error=False)


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
    if credentials is None or not hmac.compare_digest(
        credentials.credentials,
        expected,
    ):
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
    outcome = await _search_service().search(q, limit)
    return VaultSearchResponse(
        query=q,
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
        document = await documents.get_by_id(connection, document_id)

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    return _to_detail(document)
