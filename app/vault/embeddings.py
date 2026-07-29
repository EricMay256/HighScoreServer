"""Provider-neutral embedding port.

This module names what the vault needs from an embedding service and nothing
about how any particular vendor supplies it. Adapters live in their own modules
and depend on this one; nothing here may import an adapter. Dropping a vendor is
then deleting a file, not editing the port.
"""

from collections.abc import Sequence
from enum import Enum
from typing import Protocol, runtime_checkable


# A single embedding. Tuples rather than lists so a vector cannot be mutated
# after the provider returns it, matching the frozen domain records.
EmbeddingVector = tuple[float, ...]


class EmbeddingInputKind(str, Enum):
    """Which side of a retrieval pair a text belongs to.

    Asymmetric models encode a stored passage differently from the question
    asked about it, usually through an instruction prefix or an explicit input
    type. Symmetric models — OpenAI's among them — ignore the distinction. The
    port carries it regardless so that adopting an asymmetric provider is a new
    adapter rather than a signature change at every call site.
    """

    DOCUMENT = "document"
    QUERY = "query"


class EmbeddingError(RuntimeError):
    """Base class for every failure to produce an embedding."""


class EmbeddingProviderNotConfigured(EmbeddingError):
    """The selected provider is missing configuration it cannot default."""


class EmbeddingUnavailable(EmbeddingError):
    """The provider could not be reached, or refused the request.

    Raised for transport failures, rate limiting, and provider-side errors —
    conditions where the same request might succeed later. Callers map this to
    503 rather than 500.
    """


class EmbeddingDimensionMismatch(EmbeddingError):
    """The provider returned a vector the persisted schema cannot store.

    ``vault_document_embeddings.embedding`` is ``vector(1536)`` and HNSW needs a
    fixed width, so a mismatch is a deployment error to surface loudly, never a
    value to pad or truncate into place.
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The only embedding surface the rest of the vault is allowed to know."""

    @property
    def profile_id(self) -> str:
        """Stable identity of provider, model, and dimensionality together.

        This is written into every embedding row. Two vectors are comparable
        only when their profiles match.
        """

    @property
    def dimensions(self) -> int:
        """Width of the vectors this provider emits."""

    async def embed(
        self,
        texts: Sequence[str],
        kind: EmbeddingInputKind,
    ) -> tuple[EmbeddingVector, ...]:
        """Embed ``texts``, returning one vector per input in the same order."""

    async def aclose(self) -> None:
        """Release any transport resources held by the adapter."""


async def embed_one(
    provider: EmbeddingProvider,
    text: str,
    kind: EmbeddingInputKind,
) -> EmbeddingVector:
    """Embed a single text, the shape every query-side caller wants."""

    vectors = await provider.embed([text], kind)
    if len(vectors) != 1:
        raise EmbeddingUnavailable(
            f"Embedding provider returned {len(vectors)} vectors for one input"
        )
    return vectors[0]
