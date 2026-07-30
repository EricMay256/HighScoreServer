"""Read-only HTTP surface for the vault.

Search and document retrieval only. Contribution, review, and compile endpoints
belong to later phases and are deliberately absent.

Access is gated on operator-issued agent credentials
(``hssv1_<credential-id>_<secret>``), verified against
``vault_agent_credentials``. The vault cannot reuse HighScoreServer's auth —
importing it would breach the isolation rule that keeps extraction a directory
move — and the integration spec is explicit that player JWTs and the global
leaderboard ``API_KEY`` are not vault credentials.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .api_models import VaultDocumentDetail, VaultSearchHit, VaultSearchResponse
from .auth import VaultCredential, VaultScope, authorize, parse_token
from .constants import resolve_text_search_config
from .db import get_vault_engine
from .domain import DocumentStatus, VaultDocument
from .embedding_runtime import get_embedding_provider
from .repository import VaultAgentCredentialRepository, VaultDocumentRepository
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


async def require_read_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VaultCredential:
    return await _authenticated((VaultScope.READ,), credentials)


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
        related_ids=list(document.related_ids),
        source_ids=list(document.source_ids),
        source_url=document.source_url,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get(
    "/search",
    response_model=VaultSearchResponse,
    dependencies=[Depends(require_read_scope)],
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
    dependencies=[Depends(require_read_scope)],
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
