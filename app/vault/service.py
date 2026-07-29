"""Application-service transaction boundary for vault use cases."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .db import VaultPoolObserver, acquire_vault_connection
from .domain import VectorSearchStatus
from .embeddings import (
    EmbeddingError,
    EmbeddingInputKind,
    EmbeddingProvider,
    embed_one,
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
