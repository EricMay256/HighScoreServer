import asyncio
import json

import httpx
import pytest

from app.vault.constants import DEFAULT_EMBEDDING_TIMEOUT_SECONDS
from app.vault.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingInputKind,
    EmbeddingProviderNotConfigured,
    EmbeddingUnavailable,
    embed_one,
)
from app.vault.embeddings_openai import (
    _BACKOFF_BASE_SECONDS,
    _MAX_ATTEMPTS,
    _MAX_BACKOFF_SECONDS,
    OpenAIEmbeddingProvider,
)


PROFILE_ID = "openai/text-embedding-3-small:1536"
DIMENSIONS = 4


def make_provider(
    handler: object,
    **overrides: object,
) -> OpenAIEmbeddingProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
    )
    kwargs: dict[str, object] = {
        "api_key": None,
        "model": "text-embedding-3-small",
        "profile_id": PROFILE_ID,
        "dimensions": DIMENSIONS,
        "client": client,
    }
    kwargs.update(overrides)
    return OpenAIEmbeddingProvider(**kwargs)


def embedding_response(vectors: list[list[float]], shuffle: bool = False) -> httpx.Response:
    data = [
        {"index": index, "embedding": vector}
        for index, vector in enumerate(vectors)
    ]
    if shuffle:
        data = list(reversed(data))
    return httpx.Response(200, json={"data": data})


def test_api_key_is_required_when_no_client_is_supplied() -> None:
    with pytest.raises(EmbeddingProviderNotConfigured, match="VAULT_EMBEDDING_API_KEY"):
        OpenAIEmbeddingProvider(
            api_key=None,
            model="text-embedding-3-small",
            profile_id=PROFILE_ID,
            dimensions=DIMENSIONS,
        )


def test_request_states_model_and_dimensions_explicitly() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        return embedding_response([[0.1] * DIMENSIONS])

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.DOCUMENT)
        finally:
            await provider.aclose()

    asyncio.run(exercise())

    assert captured["path"].endswith("/embeddings")
    assert captured["model"] == "text-embedding-3-small"
    # Stated in the request rather than left to the model default, so the
    # persisted vector(1536) contract is explicit at the call site.
    assert captured["dimensions"] == DIMENSIONS
    assert captured["encoding_format"] == "float"
    assert captured["input"] == ["hello"]


def test_vectors_follow_input_order_not_response_order() -> None:
    # The API documents that results may arrive out of order; `index` is the
    # only reliable mapping back to the input.
    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response(
            [[0.0] * DIMENSIONS, [1.0] * DIMENSIONS, [2.0] * DIMENSIONS],
            shuffle=True,
        )

    async def exercise() -> tuple[tuple[float, ...], ...]:
        provider = make_provider(handler)
        try:
            return await provider.embed(
                ["a", "b", "c"],
                EmbeddingInputKind.DOCUMENT,
            )
        finally:
            await provider.aclose()

    vectors = asyncio.run(exercise())

    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]


def test_query_and_document_kinds_produce_identical_requests() -> None:
    # OpenAI's embedding models are symmetric. The port carries the distinction
    # for asymmetric providers; this adapter must not invent one.
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return embedding_response([[0.5] * DIMENSIONS])

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["same text"], EmbeddingInputKind.DOCUMENT)
            await provider.embed(["same text"], EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    asyncio.run(exercise())

    assert payloads[0] == payloads[1]


def test_dimension_mismatch_is_refused_rather_than_reshaped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response([[0.1] * (DIMENSIONS + 1)])

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.DOCUMENT)
        finally:
            await provider.aclose()

    with pytest.raises(EmbeddingDimensionMismatch, match="the vault schema stores"):
        asyncio.run(exercise())


def test_large_inputs_are_split_into_batches() -> None:
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch_sizes.append(len(payload["input"]))
        return embedding_response([[0.1] * DIMENSIONS] * len(payload["input"]))

    async def exercise() -> tuple[tuple[float, ...], ...]:
        provider = make_provider(handler, batch_size=2)
        try:
            return await provider.embed(
                ["a", "b", "c", "d", "e"],
                EmbeddingInputKind.DOCUMENT,
            )
        finally:
            await provider.aclose()

    vectors = asyncio.run(exercise())

    assert batch_sizes == [2, 2, 1]
    assert len(vectors) == 5


def test_retries_are_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped budget retries, because the realistic failure is transient.

    Measured single-query latency is p99 1.194s against a 5s timeout, so a
    request that fails is far more likely to have hit a 429 or a 502 than to
    have been genuinely slow. At the previous single attempt one such blip cost
    the vector arm entirely.
    """

    monkeypatch.setattr("app.vault.embeddings_openai.asyncio.sleep", _no_sleep)
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, headers={"retry-after": "1"})

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    assert _MAX_ATTEMPTS > 1
    with pytest.raises(EmbeddingUnavailable, match="HTTP 429"):
        asyncio.run(exercise())

    # Every attempt in the budget is spent before giving up.
    assert len(attempts) == _MAX_ATTEMPTS


def test_rate_limiting_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned to 2 rather than reading the shipped count, so the assertion below
    # states an exact number of attempts. Retries are live at the shipped count
    # (see test_retries_are_enabled_by_default); this covers the succeeds-on-
    # retry path specifically.
    monkeypatch.setattr("app.vault.embeddings_openai._MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        "app.vault.embeddings_openai.asyncio.sleep",
        _no_sleep,
    )
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return embedding_response([[0.3] * DIMENSIONS])

    async def exercise() -> tuple[float, ...]:
        provider = make_provider(handler)
        try:
            return await embed_one(provider, "hello", EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    vector = asyncio.run(exercise())

    assert len(attempts) == 2
    assert vector[0] == pytest.approx(0.3)


def test_persistent_rate_limiting_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.vault.embeddings_openai._MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        "app.vault.embeddings_openai.asyncio.sleep",
        _no_sleep,
    )
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429)

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    with pytest.raises(EmbeddingUnavailable):
        asyncio.run(exercise())

    # One attempt and one retry. The budget is bounded by the request the
    # caller is waiting on, not by what would eventually succeed.
    assert len(attempts) == 2


def test_client_error_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    with pytest.raises(EmbeddingUnavailable, match="HTTP 400"):
        asyncio.run(exercise())

    assert len(attempts) == 1


def test_transport_failure_becomes_embedding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.vault.embeddings_openai.asyncio.sleep",
        _no_sleep,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["hello"], EmbeddingInputKind.QUERY)
        finally:
            await provider.aclose()

    # A connect error is retryable, so the whole budget is spent. Derived from
    # the constant rather than restated, including the pluralization, so this
    # keeps describing the shipped budget if the count moves again.
    plural = "" if _MAX_ATTEMPTS == 1 else "s"
    with pytest.raises(
        EmbeddingUnavailable, match=f"after {_MAX_ATTEMPTS} attempt{plural}"
    ):
        asyncio.run(exercise())


def test_mismatched_result_count_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response([[0.1] * DIMENSIONS])

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["a", "b"], EmbeddingInputKind.DOCUMENT)
        finally:
            await provider.aclose()

    with pytest.raises(EmbeddingUnavailable, match="1 embeddings for 2 inputs"):
        asyncio.run(exercise())


def test_blank_input_is_rejected_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not call out for blank input")

    async def exercise() -> None:
        provider = make_provider(handler)
        try:
            await provider.embed(["  "], EmbeddingInputKind.DOCUMENT)
        finally:
            await provider.aclose()

    with pytest.raises(ValueError, match="must not be blank"):
        asyncio.run(exercise())


def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not call out for an empty batch")

    async def exercise() -> tuple[tuple[float, ...], ...]:
        provider = make_provider(handler)
        try:
            return await provider.embed([], EmbeddingInputKind.DOCUMENT)
        finally:
            await provider.aclose()

    assert asyncio.run(exercise()) == ()


def test_retry_after_is_honoured_but_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait tracks Retry-After only up to the configured ceiling.

    The cap is load-bearing for the worst-case budget: at the shipped attempt
    count two waits at this ceiling are 8 of the 23 seconds.
    """

    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.vault.embeddings_openai.asyncio.sleep", record)

    for retry_after, expected in (
        (None, _BACKOFF_BASE_SECONDS),  # plain exponential backoff
        ("2", 2.0),  # longer than the base wait, so it wins
        ("0", _BACKOFF_BASE_SECONDS),  # shorter, so the base wait wins
        ("3600", _MAX_BACKOFF_SECONDS),  # capped, not obeyed
        ("Wed, 21 Oct 2026 07:28:00 GMT", _BACKOFF_BASE_SECONDS),  # HTTP-date
    ):
        slept.clear()
        asyncio.run(OpenAIEmbeddingProvider._sleep_before_retry(1, retry_after))
        assert slept == [expected], f"retry_after={retry_after!r}"


def test_worst_case_retry_budget_fits_inside_the_router_timeout() -> None:
    # Every attempt can burn a full request timeout, and a wait separates each
    # pair of them — so N attempts means N timeouts and N-1 waits. Heroku's
    # router gives up at 30s, and a degraded answer that never gets returned is
    # worth nothing. Guards the constants against being raised past the budget.
    #
    # Read from configuration rather than restated here: a hardcoded 10.0 would
    # keep passing after the configured default moved, leaving this modelling a
    # budget nothing actually runs under.
    worst_case = _MAX_ATTEMPTS * DEFAULT_EMBEDDING_TIMEOUT_SECONDS + (
        (_MAX_ATTEMPTS - 1) * _MAX_BACKOFF_SECONDS
    )

    assert worst_case <= 30.0


async def _no_sleep(_seconds: float) -> None:
    return None
