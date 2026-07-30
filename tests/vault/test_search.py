import asyncio
from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.db import create_vault_engine
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    VectorSearchStatus,
)
from app.vault.embeddings import (
    EmbeddingInputKind,
    EmbeddingUnavailable,
    EmbeddingVector,
)
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
)
from app.vault.search import (
    RRF_K,
    VaultSearchRepository,
    candidate_depth,
    reciprocal_rank_fusion,
)
from app.vault.service import VaultSearchService, VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_documents


PROFILE_ID = "test/fixture-model:1536"


# ---------------------------------------------------------------- fusion ----
# Pure ranking logic, so these need no database and no pgvector.


def test_fusion_scores_a_single_arm_by_position() -> None:
    fused = reciprocal_rank_fusion(["a", "b"], [])

    assert [hit.document_id for hit in fused] == ["a", "b"]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))
    assert fused[1].score == pytest.approx(1 / (RRF_K + 2))
    assert fused[0].lexical_rank == 1
    assert fused[0].vector_rank is None


def test_agreement_between_arms_outranks_either_arm_alone() -> None:
    # "b" is second in both rankings; "a" is first in one and absent from the
    # other. Two mediocre votes beating one strong vote is the whole point of
    # running both arms.
    fused = reciprocal_rank_fusion(["a", "b"], ["c", "b"])

    assert fused[0].document_id == "b"
    assert fused[0].lexical_rank == 2
    assert fused[0].vector_rank == 2
    assert fused[0].score == pytest.approx(2 / (RRF_K + 2))


def test_duplicate_ids_within_one_arm_are_counted_once() -> None:
    fused = reciprocal_rank_fusion(["a", "a"], [])

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))


def test_equal_scores_break_ties_by_id_for_stable_output() -> None:
    first = reciprocal_rank_fusion(["b"], ["a"])
    second = reciprocal_rank_fusion(["b"], ["a"])

    assert [hit.document_id for hit in first] == ["a", "b"]
    assert first == second


def test_fusion_rejects_a_nonpositive_constant() -> None:
    with pytest.raises(ValueError, match="k must be one or greater"):
        reciprocal_rank_fusion(["a"], [], k=0)


def test_candidate_depth_oversamples_but_is_bounded() -> None:
    assert candidate_depth(10) == 40
    assert candidate_depth(100) == 200


# ------------------------------------------------------------- fixtures ----


class StubEmbeddingProvider:
    """Deterministic stand-in so retrieval tests never call a vendor."""

    def __init__(
        self,
        vectors: dict[str, EmbeddingVector],
        profile_id: str = PROFILE_ID,
    ) -> None:
        self._vectors = vectors
        self._profile_id = profile_id
        self.calls: list[EmbeddingInputKind] = []

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    async def embed(
        self,
        texts: Sequence[str],
        kind: EmbeddingInputKind,
    ) -> tuple[EmbeddingVector, ...]:
        self.calls.append(kind)
        return tuple(self._vectors[text] for text in texts)

    async def aclose(self) -> None:
        return None


class FailingEmbeddingProvider(StubEmbeddingProvider):
    async def embed(
        self,
        texts: Sequence[str],
        kind: EmbeddingInputKind,
    ) -> tuple[EmbeddingVector, ...]:
        raise EmbeddingUnavailable("provider is down")


def basis_vector(axis: int) -> EmbeddingVector:
    """A unit vector along one axis, so cosine ordering is obvious by eye."""

    return tuple(1.0 if index == axis else 0.0 for index in range(EMBEDDING_DIMENSIONS))


CORPUS = (
    # (suffix, title, body, axis, status)
    ("alpha", "Postgres indexing", "GIN indexes accelerate running queries.", 0, DocumentStatus.ACTIVE),
    ("beta", "Vector similarity", "HNSW graphs approximate nearest neighbours.", 1, DocumentStatus.ACTIVE),
    ("gamma", "Archived note", "GIN indexes are mentioned here too.", 2, DocumentStatus.ARCHIVED),
    ("delta", "Flagged draft", "GIN indexes are discussed in this unreviewed draft.", 3, DocumentStatus.FLAGGED),
)


async def seed_corpus(service: VaultTransactionService, run_id: str) -> dict[str, str]:
    documents = VaultDocumentRepository()
    embeddings = VaultDocumentEmbeddingRepository()
    ids: dict[str, str] = {}

    async with service.transaction() as connection:
        for suffix, title, body, axis, status in CORPUS:
            document_id = f"search-{run_id}-{suffix}"
            ids[suffix] = document_id
            await documents.insert(
                connection,
                NewVaultDocument(
                    id=document_id,
                    kind=DocumentKind.NOTE,
                    vault_path=f"Agent/notes/{document_id}.md",
                    status=status,
                    title=title,
                    body=body,
                    contributed_by="test:read-only-slice",
                    provenance={"fixture": True},
                ),
            )
            await embeddings.upsert(
                connection,
                DocumentEmbedding(
                    document_id=document_id,
                    profile_id=PROFILE_ID,
                    vector=basis_vector(axis),
                ),
            )
    return ids


async def clear_corpus(service: VaultTransactionService, ids: dict[str, str]) -> None:
    async with service.transaction() as connection:
        # Embeddings go with the document via ON DELETE CASCADE.
        await connection.execute(
            delete(vault_documents).where(vault_documents.c.id.in_(list(ids.values())))
        )


def vault_service() -> tuple[VaultTransactionService, object]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    engine, observer = create_vault_engine(settings)
    return VaultTransactionService(engine, observer), engine


# ----------------------------------------------------------- retrieval ----


def test_lexical_search_uses_the_stored_configurations_stemming(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                # "running" stems to "run" under english, which is what the
                # generated column stored.
                english = await repository.lexical_search(
                    connection,
                    query="running",
                    text_search_config="english",
                    limit=10,
                )
                # The same query under "simple" does not stem, so it cannot
                # match the stored vector. This is why the configuration is a
                # bound parameter rather than the database default.
                simple = await repository.lexical_search(
                    connection,
                    query="running",
                    text_search_config="simple",
                    limit=10,
                )

            assert [hit.document_id for hit in english] == [ids["alpha"]]
            assert simple == []
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_an_alias_makes_a_document_lexically_findable(
    configure_test_env: None,
) -> None:
    """An alias is searchable, and it is stemmed like the title.

    This is the whole payoff of putting aliases in ``search_vector``: a note
    titled "PostgreSQL" has to be findable by someone who types "Postgres".

    It also pins the reason ``vault.text_array_to_string`` exists.
    ``array_to_string`` is STABLE and PostgreSQL rejects it in a generated
    column outright; ``array_to_tsvector`` is IMMUTABLE but emits raw lexemes
    ('Postgres'), which never match a stemmed query side ('postgr') — so it
    would compile and then silently fail to match. That failure would look
    exactly like this test passing on the title alone, which is why the query
    below appears in no other field.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        documents = VaultDocumentRepository()
        repository = VaultSearchRepository()
        document_id = f"alias-{uuid4().hex}"

        try:
            async with service.transaction() as connection:
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.NOTE,
                        vault_path=f"Agent/notes/{document_id}.md",
                        status=DocumentStatus.ACTIVE,
                        title="Relational database engines",
                        # The only place "Postgres" appears.
                        aliases=("Postgres", "Postgres DB"),
                        body="An overview of storage engines and their tradeoffs.",
                        contributed_by="test:aliases",
                        provenance={"fixture": True},
                    ),
                )

            async with service.transaction() as connection:
                hits = await repository.lexical_search(
                    connection,
                    query="postgres",
                    text_search_config="english",
                    limit=10,
                )

            assert [hit.document_id for hit in hits] == [document_id]

            async with service.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id == document_id
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_lexical_search_excludes_documents_that_are_not_active(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                hits = await repository.lexical_search(
                    connection,
                    query="GIN indexes",
                    text_search_config="english",
                    limit=10,
                )

            found = {hit.document_id for hit in hits}
            assert ids["alpha"] in found
            # "gamma" matches the text but is archived.
            assert ids["gamma"] not in found
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_lexical_search_disjoins_terms_so_long_queries_still_match(
    configure_test_env: None,
) -> None:
    """The behaviour vault ADR 0007 changed.

    No document contains all of gin/index/hnsw/graph, so websearch's
    conjunctive reading matched nothing at all. Disjoined, the query finds
    both documents that share vocabulary with it.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                hits = await repository.lexical_search(
                    connection,
                    query="GIN indexes and HNSW graphs",
                    text_search_config="english",
                    limit=10,
                )

            assert {hit.document_id for hit in hits} == {ids["alpha"], ids["beta"]}
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_lexical_search_keeps_quoted_phrases_intact(
    configure_test_env: None,
) -> None:
    """Disjunction rewrites conjunctions only; phrase distance survives.

    The reversed phrase is the discriminating half: were the phrase operator
    disjoined along with the conjunctions, word order would stop mattering and
    "neighbours nearest" would match too.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                in_order = await repository.lexical_search(
                    connection,
                    query='"nearest neighbours"',
                    text_search_config="english",
                    limit=10,
                )
                reversed_order = await repository.lexical_search(
                    connection,
                    query='"neighbours nearest"',
                    text_search_config="english",
                    limit=10,
                )

            assert [hit.document_id for hit in in_order] == [ids["beta"]]
            assert reversed_order == []
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_lexical_search_keeps_negation_conjunctive(
    configure_test_env: None,
) -> None:
    """A negating query opts out of disjunction.

    "indexes -GIN" excludes the one document holding both terms. Disjoined to
    'index' | !'gin' it would instead match every document lacking "GIN" —
    here, "beta" — which inverts what the caller asked for.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                hits = await repository.lexical_search(
                    connection,
                    query="indexes -GIN",
                    text_search_config="english",
                    limit=10,
                )

            assert hits == []
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_get_by_id_filters_only_when_the_caller_asks(
    configure_test_env: None,
) -> None:
    """Which statuses are visible is the surface's policy, not persistence's.

    Review tooling has to load a flagged document precisely because it is
    flagged, so the repository stays unfiltered by default and the read surface
    passes its own restriction.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        documents = VaultDocumentRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                unfiltered = await documents.get_by_id(connection, ids["delta"])
                restricted = await documents.get_by_id(
                    connection,
                    ids["delta"],
                    statuses=(DocumentStatus.ACTIVE, DocumentStatus.ARCHIVED),
                )

            assert unfiltered is not None
            assert unfiltered.status is DocumentStatus.FLAGGED
            assert restricted is None
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_vector_search_orders_by_cosine_proximity(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                hits = await repository.vector_search(
                    connection,
                    embedding=basis_vector(1),
                    profile_id=PROFILE_ID,
                    limit=10,
                )

            ordered = [hit.document_id for hit in hits]
            assert ordered[0] == ids["beta"]
            assert ids["gamma"] not in ordered
            # Reported as similarity: an exact match scores 1.0.
            assert hits[0].score == pytest.approx(1.0)
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_vector_search_is_scoped_to_one_profile(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)

        try:
            async with service.transaction() as connection:
                hits = await repository.vector_search(
                    connection,
                    embedding=basis_vector(1),
                    profile_id="some/other-model:1536",
                    limit=10,
                )

            assert hits == []
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_vector_search_refuses_a_wrongly_sized_query_vector(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        repository = VaultSearchRepository()
        try:
            async with service.transaction() as connection:
                await repository.vector_search(
                    connection,
                    embedding=(0.1, 0.2),
                    profile_id=PROFILE_ID,
                    limit=10,
                )
        finally:
            await engine.dispose()

    with pytest.raises(ValueError, match="the vault schema stores"):
        asyncio.run(exercise())


def test_hybrid_search_combines_both_arms(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        # The query matches "alpha" lexically and "beta" by vector, so a
        # working hybrid returns both where either arm alone would return one.
        provider = StubEmbeddingProvider({"running": basis_vector(1)})
        search = VaultSearchService(
            transactions=service,
            provider=provider,
            text_search_config="english",
        )

        try:
            outcome = await search.search("running", limit=10)

            found = [result.document.id for result in outcome.results]
            assert set(found) == {ids["alpha"], ids["beta"]}
            assert outcome.vector_status is VectorSearchStatus.USED
            assert outcome.profile_id == PROFILE_ID
            assert provider.calls == [EmbeddingInputKind.QUERY]

            by_id = {result.document.id: result for result in outcome.results}
            # kNN has no relevance floor: every active embedded document is a
            # candidate, so "alpha" is also ranked by the vector arm, just
            # behind the exact match.
            assert by_id[ids["beta"]].vector_rank == 1
            assert by_id[ids["beta"]].lexical_rank is None
            assert by_id[ids["alpha"]].lexical_rank == 1
            assert by_id[ids["alpha"]].vector_rank == 2
            # Ranked by both arms, so it wins the fusion outright.
            assert outcome.results[0].document.id == ids["alpha"]
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_provider_failure_degrades_to_lexical_instead_of_failing(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        search = VaultSearchService(
            transactions=service,
            provider=FailingEmbeddingProvider({}),
            text_search_config="english",
        )

        try:
            outcome = await search.search("running", limit=10)

            assert [result.document.id for result in outcome.results] == [ids["alpha"]]
            # A configured provider that broke must be distinguishable from a
            # deployment that never had one — this is a fault, not a mode.
            assert outcome.vector_status is VectorSearchStatus.FAILED
            # profile_id still reports which profile *would* have been used,
            # which is what makes the failure actionable.
            assert outcome.profile_id == PROFILE_ID
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_provider_failure_is_logged_as_an_error_without_the_query(
    configure_test_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        search = VaultSearchService(
            transactions=service,
            provider=FailingEmbeddingProvider({}),
            text_search_config="english",
        )

        try:
            with caplog.at_level("DEBUG", logger="app.vault.service"):
                await search.search("secret-query-text", limit=10)
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())

    failures = [
        record for record in caplog.records if record.levelname == "ERROR"
    ]
    assert len(failures) == 1
    # The exception type is recorded so the fault is diagnosable...
    assert failures[0].vault_embedding_error == "EmbeddingUnavailable"
    assert failures[0].vault_embedding_profile_id == PROFILE_ID
    # ...but the query is user content and must not reach the logs, directly
    # or via an exception message that quoted it.
    assert "secret-query-text" not in caplog.text


def test_missing_provider_is_not_logged_as_an_error(
    configure_test_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        search = VaultSearchService(
            transactions=service,
            provider=None,
            text_search_config="english",
        )

        try:
            with caplog.at_level("DEBUG", logger="app.vault.service"):
                await search.search("running", limit=10)
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())

    # A deliberate lexical-only deployment must not generate error noise on
    # every single search, or the real failures stop being visible.
    assert [record for record in caplog.records if record.levelname == "ERROR"] == []


def test_search_without_a_provider_is_lexical_only(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        search = VaultSearchService(
            transactions=service,
            provider=None,
            text_search_config="english",
        )

        try:
            outcome = await search.search("running", limit=10)

            assert [result.document.id for result in outcome.results] == [ids["alpha"]]
            # Absence of a provider is a supported mode, not a failure.
            assert outcome.vector_status is VectorSearchStatus.NOT_CONFIGURED
            assert outcome.profile_id is None
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())


def test_search_limit_is_applied_after_fusion(
    configure_test_env: None,
) -> None:
    async def exercise() -> None:
        service, engine = vault_service()
        run_id = uuid4().hex
        ids = await seed_corpus(service, run_id)
        provider = StubEmbeddingProvider({"running": basis_vector(1)})
        search = VaultSearchService(
            transactions=service,
            provider=provider,
            text_search_config="english",
        )

        try:
            outcome = await search.search("running", limit=1)

            assert len(outcome.results) == 1
        finally:
            await clear_corpus(service, ids)
            await engine.dispose()

    asyncio.run(exercise())
