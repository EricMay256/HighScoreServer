"""Adapter registry and worker-local embedding-provider lifecycle.

This is the one module that knows which concrete adapters exist. The port in
``embeddings.py`` stays free of vendor names, and ``settings.py`` stays free of
adapter imports, so configuration parsing does not drag a transport client into
the Alembic environment.

Lifecycle mirrors ``db.py``: one provider per worker process, created during
startup and closed during shutdown.
"""

from collections.abc import Callable
import logging

from .embeddings import EmbeddingProvider, EmbeddingProviderNotConfigured
from .embeddings_openai import OpenAIEmbeddingProvider
from .settings import EmbeddingSettings


logger = logging.getLogger(__name__)


def _create_openai(settings: EmbeddingSettings) -> EmbeddingProvider:
    return OpenAIEmbeddingProvider(
        api_key=settings.api_key,
        model=settings.model,
        profile_id=settings.profile_id,
        dimensions=settings.embedding_dimensions,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )


# Adding a provider is one entry here plus one adapter module. Nothing else in
# the package changes.
_FACTORIES: dict[str, Callable[[EmbeddingSettings], EmbeddingProvider]] = {
    "openai": _create_openai,
}


class UnknownEmbeddingProviderError(RuntimeError):
    """The configured provider has no adapter in this build."""


def known_embedding_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def create_embedding_provider(settings: EmbeddingSettings) -> EmbeddingProvider:
    """Build the adapter named by ``settings.provider``."""

    factory = _FACTORIES.get(settings.provider)
    if factory is None:
        known = ", ".join(known_embedding_providers())
        raise UnknownEmbeddingProviderError(
            f"VAULT_EMBEDDING_PROVIDER={settings.provider!r} has no adapter. "
            f"Known providers: {known}."
        )
    return factory(settings)


_provider: EmbeddingProvider | None = None


async def init_vault_embeddings() -> None:
    """Create this worker's embedding provider, if one is configured.

    A missing credential is a supported state, not a failure: the vault still
    serves lexical search, which needs no third party. CI and local development
    run this way. Anything else wrong with the configuration — an unknown
    provider name, a profile that violates the schema's format — still raises,
    because those are mistakes rather than deliberate omissions.
    """

    global _provider
    if _provider is not None:
        raise RuntimeError("Vault embedding provider is already initialized")

    settings = EmbeddingSettings.from_environment()
    try:
        provider = create_embedding_provider(settings)
    except EmbeddingProviderNotConfigured as exc:
        # Loud enough to notice in production, quiet enough not to fail a test
        # run that never intended to embed anything.
        logger.warning(
            "Vault embedding provider not configured; vector retrieval is "
            "disabled and search will be lexical only (%s)",
            exc,
        )
        return

    _provider = provider
    # profile_id is not a secret; the API key must never be logged.
    logger.info(
        "Vault embedding provider initialized",
        extra={
            "vault_embedding_provider": settings.provider,
            "vault_embedding_profile_id": provider.profile_id,
            "vault_embedding_dimensions": provider.dimensions,
        },
    )


def get_embedding_provider() -> EmbeddingProvider | None:
    """The process's provider, or None when none is configured.

    Optional by contract: callers fall back to lexical retrieval rather than
    treating absence as an error.
    """

    return _provider


async def close_vault_embeddings() -> None:
    global _provider
    if _provider is not None:
        await _provider.aclose()
        _provider = None
