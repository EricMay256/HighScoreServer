"""OpenAI embeddings adapter.

The only module in this package that knows OpenAI exists. It speaks the REST
endpoint over ``httpx``, which HighScoreServer already depends on, rather than
the vendor SDK: the call is one POST with a JSON body, so the SDK would add a
dependency and an extra async client for no benefit. See vault ADR 0005.

Everything OpenAI-specific — the request field names, the batch ceiling, the
retry rules — stops here. The rest of the vault sees only the port in
``embeddings.py``.
"""

import asyncio
import logging
from collections.abc import Sequence

import httpx

from .constants import DEFAULT_EMBEDDING_TIMEOUT_SECONDS
from .embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingInputKind,
    EmbeddingProviderNotConfigured,
    EmbeddingUnavailable,
    EmbeddingVector,
)


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# The API accepts up to 2048 inputs per request but also caps total tokens per
# request, so a full 2048-item batch of long documents is rejected on size
# rather than count. A smaller default stays clear of that ceiling; callers
# embedding short texts can raise it.
DEFAULT_BATCH_SIZE = 128
MAX_BATCH_SIZE = 2048

_RETRY_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})

# Three attempts at a 5s timeout. Worst case 3 x 5s + 2 x 4s of capped backoff
# = 23s, inside Heroku's 30s router budget with room for the search itself.
#
# The budget is bounded by the request the caller is waiting on, not by what
# would most likely eventually succeed. A query embedding sits in the middle of
# a search, so exhausting the budget is not a failure but a fall back to lexical
# results — worth having only while someone is still waiting.
#
# Settled by measurement on 2026-08-12, replacing one attempt at 10s. Observed
# single-query latency is p50 0.163s / p99 1.194s, so genuine slowness is not
# the failure mode this budget has to survive; a transient 429 or 502 is, and at
# one attempt a single blip cost the vector arm entirely. That is not
# hypothetical — it happened twice while taking these very measurements, once
# aborting a full calibration run. A 5s ceiling still sits ~4x above the
# observed p99, so it does not turn slow-but-successful calls into failures.
#
# The arithmetic is an upper bound rather than a guarantee: httpx's read timeout
# is per-chunk, not total request duration. See "Deferred decisions" item 3 in
# docs/vault-architecture.md.
#
# A batch backfill should still set its own, longer values — it has no caller
# waiting on it and should prefer eventual success over latency.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
# Caps any Retry-After the provider sends: on a request path a long rate-limit
# window helps nobody once the caller has gone, and a backfill should set its
# own value. Load-bearing for the worst-case budget above — two waits at this
# cap are 8 of the 23 seconds.
_MAX_BACKOFF_SECONDS = 4.0


class OpenAIEmbeddingProvider:
    """Embeddings from OpenAI's ``/embeddings`` endpoint.

    OpenAI's embedding models are symmetric — a query and a document are encoded
    by the same call — so ``kind`` is accepted and deliberately ignored. That is
    a property of this vendor, not of the port.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        profile_id: str,
        dimensions: int,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key and client is None:
            raise EmbeddingProviderNotConfigured(
                "VAULT_EMBEDDING_API_KEY is required for the 'openai' provider"
            )
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")

        self._model = model
        self._profile_id = profile_id
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=(base_url or DEFAULT_BASE_URL).rstrip("/"),
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(
        self,
        texts: Sequence[str],
        kind: EmbeddingInputKind,
    ) -> tuple[EmbeddingVector, ...]:
        del kind  # Symmetric model: both sides use the identical request.

        if not texts:
            return ()
        if any(not text.strip() for text in texts):
            raise ValueError("Embedding inputs must not be blank")

        vectors: list[EmbeddingVector] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return tuple(vectors)

    async def _embed_batch(
        self,
        batch: Sequence[str],
    ) -> list[EmbeddingVector]:
        payload = {
            "model": self._model,
            "input": list(batch),
            # Sent explicitly rather than relying on the model default so the
            # persisted vector(1536) contract is stated in the request itself.
            # This requires a v3 embedding model; ada-002 rejects the field.
            "dimensions": self._dimensions,
            "encoding_format": "float",
        }
        response = await self._post_with_retries(payload, len(batch))

        try:
            body = response.json()
            data = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingUnavailable(
                "OpenAI embeddings response was not in the expected shape"
            ) from exc

        if len(data) != len(batch):
            raise EmbeddingUnavailable(
                f"OpenAI returned {len(data)} embeddings for {len(batch)} inputs"
            )

        # The API documents that results may arrive out of order, so `index` is
        # the authority on which input a vector belongs to — never list position.
        try:
            ordered = sorted(data, key=lambda item: item["index"])
        except (KeyError, TypeError) as exc:
            raise EmbeddingUnavailable(
                "OpenAI embeddings response omitted the input index"
            ) from exc

        vectors: list[EmbeddingVector] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list):
                raise EmbeddingUnavailable(
                    "OpenAI embeddings response omitted an embedding vector"
                )
            if len(vector) != self._dimensions:
                raise EmbeddingDimensionMismatch(
                    f"OpenAI model {self._model!r} returned {len(vector)} "
                    f"dimensions; the vault schema stores {self._dimensions}"
                )
            vectors.append(tuple(float(value) for value in vector))
        return vectors

    async def _post_with_retries(
        self,
        payload: dict[str, object],
        batch_size: int,
    ) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post("/embeddings", json=payload)
            except httpx.HTTPError as exc:
                # Transport failure: no response to inspect, so retry blindly
                # within the attempt budget.
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    break
                await self._sleep_before_retry(attempt, None)
                continue

            if response.status_code < 400:
                return response

            if (
                response.status_code in _RETRY_STATUS_CODES
                and attempt < _MAX_ATTEMPTS
            ):
                # Log status only. Request bodies carry note content and the
                # headers carry the API key; neither may reach the logs.
                logger.warning(
                    "Retrying OpenAI embeddings request",
                    extra={
                        "status_code": response.status_code,
                        "attempt": attempt,
                        "batch_size": batch_size,
                    },
                )
                await self._sleep_before_retry(
                    attempt,
                    response.headers.get("retry-after"),
                )
                continue

            raise EmbeddingUnavailable(
                f"OpenAI embeddings request failed with HTTP "
                f"{response.status_code}"
            )

        raise EmbeddingUnavailable(
            "OpenAI embeddings request failed after "
            f"{_MAX_ATTEMPTS} attempt{'' if _MAX_ATTEMPTS == 1 else 's'}"
        ) from last_error

    @staticmethod
    async def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        if retry_after:
            try:
                # Only the delta-seconds form is honoured; an HTTP-date would
                # need clock-skew handling for no practical gain here.
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        await asyncio.sleep(min(delay, _MAX_BACKOFF_SECONDS))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
