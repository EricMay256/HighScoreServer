"""Hybrid lexical + vector retrieval over the vault corpus.

Two rankings are produced independently — PostgreSQL full-text search over the
stored ``search_vector`` column, and cosine similarity over
``vault_document_embeddings`` — then combined with Reciprocal Rank Fusion.

RRF is used rather than a weighted sum of scores because ``ts_rank_cd`` and
cosine distance are not on comparable scales and neither is calibrated across
queries. Fusing positions instead of magnitudes needs no per-query tuning, which
is the property that matters while the corpus is small and no relevance
judgements exist to tune against.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Text, bindparam, case, cast, func, select
from sqlalchemy.dialects.postgresql import REGCONFIG, TSQUERY
from sqlalchemy.ext.asyncio import AsyncConnection

from .constants import EMBEDDING_DIMENSIONS
from .domain import DocumentStatus, VaultDocument
from .embeddings import EmbeddingVector
from .repository import DOCUMENT_DOMAIN_COLUMNS, document_from_row
from .tables import vault_document_embeddings, vault_documents


# The standard RRF constant from Cormack et al. It damps the influence of the
# very top positions so one ranker cannot dominate the fused order outright.
RRF_K = 60

# Each retrieval arm fetches deeper than the requested page so that a document
# ranked highly by only one arm still survives fusion.
DEFAULT_CANDIDATE_MULTIPLIER = 4
MAX_CANDIDATES = 200


@dataclass(frozen=True, slots=True)
class ScoredDocumentId:
    """One arm's opinion about one document."""

    document_id: str
    score: float


@dataclass(frozen=True, slots=True)
class FusedHit:
    """A document's position after fusion, with the evidence behind it."""

    document_id: str
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A fused hit joined back to its document."""

    document: VaultDocument
    score: float
    lexical_rank: int | None
    vector_rank: int | None


def reciprocal_rank_fusion(
    lexical: Sequence[str],
    vector: Sequence[str],
    *,
    k: int = RRF_K,
) -> list[FusedHit]:
    """Fuse two ranked ID lists into one.

    Each list contributes ``1 / (k + rank)`` for the documents it ranks, with
    ``rank`` starting at 1. A document found by both arms outscores one found by
    either alone at the same depth, which is the behaviour that makes hybrid
    retrieval worth running.
    """

    if k < 1:
        raise ValueError("RRF k must be one or greater")

    contributions: dict[str, float] = {}
    lexical_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}

    for ranks, ranking in ((lexical_ranks, lexical), (vector_ranks, vector)):
        for position, document_id in enumerate(ranking, start=1):
            # A duplicate ID within one arm keeps its best position; scoring it
            # twice would let one arm outvote the other.
            if document_id in ranks:
                continue
            ranks[document_id] = position
            contributions[document_id] = contributions.get(document_id, 0.0) + 1.0 / (
                k + position
            )

    fused = [
        FusedHit(
            document_id=document_id,
            score=score,
            lexical_rank=lexical_ranks.get(document_id),
            vector_rank=vector_ranks.get(document_id),
        )
        for document_id, score in contributions.items()
    ]
    # Ties broken by ID so identical scores produce a stable, testable order.
    fused.sort(key=lambda hit: (-hit.score, hit.document_id))
    return fused


class VaultSearchRepository:
    """Read-only retrieval queries. Connection-injected, like every repository."""

    # Bound, never interpolated: the configuration must be the same one the
    # generated column was built with, and a bind parameter cannot be a SQL
    # injection surface the way an f-string could.
    _websearch = func.websearch_to_tsquery(
        cast(bindparam("text_search_config", type_=Text), REGCONFIG),
        bindparam("query", type_=Text),
    )

    # websearch_to_tsquery conjoins every term, so a document must contain all
    # of them to match at all. Recall therefore falls away as the query gets
    # longer, and questions — what this corpus is asked — are long. Disjoining
    # the terms makes the lexical arm answer "which documents share vocabulary
    # with this query", and ts_rank_cd plus RRF's position-based scoring sort
    # out how much each one is worth. See vault ADR 0007.
    #
    # Rewriting the parsed query's text rather than re-lexing the raw string is
    # what preserves quoted phrases: websearch renders those with <-> , which
    # is left untouched. Only the top-level conjunctions become disjunctions.
    # This is safe because the text-search parser never puts a space inside a
    # lexeme, so the literal " & " separator cannot occur within one.
    _disjoined = cast(
        func.regexp_replace(cast(_websearch, Text), " & ", " | ", "g"),
        TSQUERY,
    )

    # Negation is the exception. "!'a' | 'b'" matches every document lacking
    # "a", which is close to the whole corpus — disjoining an exclusion inverts
    # what the caller asked for. A query that negates keeps websearch's own
    # conjunctive reading.
    _tsquery = case(
        (func.strpos(cast(_websearch, Text), "!") > 0, _websearch),
        else_=_disjoined,
    )

    async def lexical_search(
        self,
        connection: AsyncConnection,
        *,
        query: str,
        text_search_config: str,
        limit: int,
    ) -> list[ScoredDocumentId]:
        """Rank active documents by full-text relevance."""

        rank = func.ts_rank_cd(vault_documents.c.search_vector, self._tsquery)
        statement = (
            select(vault_documents.c.id, rank.label("score"))
            .where(
                vault_documents.c.search_vector.op("@@")(self._tsquery),
                vault_documents.c.status == DocumentStatus.ACTIVE.value,
            )
            .order_by(rank.desc(), vault_documents.c.id)
            .limit(bindparam("row_limit"))
        )
        result = await connection.execute(
            statement,
            {
                "text_search_config": text_search_config,
                "query": query,
                "row_limit": limit,
            },
        )
        return [
            ScoredDocumentId(document_id=row["id"], score=float(row["score"]))
            for row in result.mappings()
        ]

    async def vector_search(
        self,
        connection: AsyncConnection,
        *,
        embedding: EmbeddingVector,
        profile_id: str,
        limit: int,
    ) -> list[ScoredDocumentId]:
        """Rank active documents by cosine similarity under one profile.

        The HNSW index is unpartitioned, so the ``profile_id`` predicate is a
        post-filter: with a second profile populated this can return fewer rows
        than requested. Vault ADR 0003 records the remedy — a partial index per
        profile — as a migration for when that day arrives.
        """

        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Query embedding has {len(embedding)} dimensions; "
                f"the vault schema stores {EMBEDDING_DIMENSIONS}"
            )

        distance = vault_document_embeddings.c.embedding.cosine_distance(
            list(embedding)
        )
        statement = (
            select(
                vault_document_embeddings.c.document_id,
                distance.label("distance"),
            )
            .select_from(
                vault_document_embeddings.join(
                    vault_documents,
                    vault_documents.c.id == vault_document_embeddings.c.document_id,
                )
            )
            .where(
                vault_document_embeddings.c.profile_id == profile_id,
                vault_documents.c.status == DocumentStatus.ACTIVE.value,
            )
            .order_by(distance, vault_document_embeddings.c.document_id)
            .limit(limit)
        )
        result = await connection.execute(statement)
        return [
            # Reported as similarity so a larger score is a better match in
            # both arms; cosine distance runs the other way.
            ScoredDocumentId(
                document_id=row["document_id"],
                score=1.0 - float(row["distance"]),
            )
            for row in result.mappings()
        ]

    async def fetch_documents(
        self,
        connection: AsyncConnection,
        document_ids: Sequence[str],
    ) -> dict[str, VaultDocument]:
        """Load documents by ID, keyed for re-ordering by the caller."""

        if not document_ids:
            return {}

        statement = select(*DOCUMENT_DOMAIN_COLUMNS).where(
            vault_documents.c.id.in_(list(document_ids))
        )
        result = await connection.execute(statement)
        return {row["id"]: document_from_row(row) for row in result.mappings()}


def candidate_depth(limit: int, multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER) -> int:
    """How deep each arm should search to give fusion something to work with."""

    return min(max(limit * multiplier, limit), MAX_CANDIDATES)


def document_ids(scored: Iterable[ScoredDocumentId]) -> list[str]:
    return [item.document_id for item in scored]
