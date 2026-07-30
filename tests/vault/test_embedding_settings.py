import pytest

from app.vault.constants import DEFAULT_EMBEDDING_TIMEOUT_SECONDS, EMBEDDING_DIMENSIONS
from app.vault.embedding_runtime import (
    UnknownEmbeddingProviderError,
    create_embedding_provider,
    known_embedding_providers,
)
from app.vault.embeddings import EmbeddingProviderNotConfigured
from app.vault.settings import EmbeddingSettings


def test_openai_is_the_registered_provider() -> None:
    assert known_embedding_providers() == ("openai",)


def test_default_profile_names_provider_model_and_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAULT_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("VAULT_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("VAULT_EMBEDDING_PROFILE_ID", raising=False)

    settings = EmbeddingSettings.from_environment()

    assert settings.provider == "openai"
    assert settings.model == "text-embedding-3-small"
    assert settings.profile_id == (
        f"openai/text-embedding-3-small:{EMBEDDING_DIMENSIONS}"
    )


def test_unset_timeout_falls_back_to_the_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closes the loop on the retry-budget test.

    ``test_worst_case_retry_budget_fits_inside_the_router_timeout`` models the
    worst case using ``DEFAULT_EMBEDDING_TIMEOUT_SECONDS``. That is only
    meaningful if the value a real process ends up with is the same one, so
    this pins settings to the shared constant rather than to a literal.
    """

    monkeypatch.delenv("VAULT_EMBEDDING_TIMEOUT_SECONDS", raising=False)

    settings = EmbeddingSettings.from_environment()

    assert settings.timeout_seconds == DEFAULT_EMBEDDING_TIMEOUT_SECONDS


def test_default_profile_follows_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Settings no longer hardcode a vendor, so the derived profile tracks
    # whatever provider is configured.
    monkeypatch.setenv("VAULT_EMBEDDING_PROVIDER", "cohere")
    monkeypatch.setenv("VAULT_EMBEDDING_MODEL", "embed-v4.0")
    monkeypatch.delenv("VAULT_EMBEDDING_PROFILE_ID", raising=False)

    settings = EmbeddingSettings.from_environment()

    assert settings.profile_id == f"cohere/embed-v4.0:{EMBEDDING_DIMENSIONS}"


def test_settings_do_not_reject_a_provider_they_cannot_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Parsing is separate from adapter availability; that separation keeps the
    # Alembic environment free of transport imports.
    monkeypatch.setenv("VAULT_EMBEDDING_PROVIDER", "cohere")

    settings = EmbeddingSettings.from_environment()

    with pytest.raises(UnknownEmbeddingProviderError, match="Known providers: openai"):
        create_embedding_provider(settings)


def test_base_url_is_unset_by_default_so_the_adapter_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAULT_EMBEDDING_BASE_URL", raising=False)

    assert EmbeddingSettings.from_environment().base_url is None


def test_base_url_override_loses_its_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_EMBEDDING_BASE_URL", "https://proxy.example.test/v1/")

    assert EmbeddingSettings.from_environment().base_url == (
        "https://proxy.example.test/v1"
    )


def test_missing_api_key_is_reported_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("VAULT_EMBEDDING_API_KEY", raising=False)

    settings = EmbeddingSettings.from_environment()

    # A distinct exception type, because a missing credential is a supported
    # lexical-only deployment rather than a broken configuration.
    with pytest.raises(EmbeddingProviderNotConfigured):
        create_embedding_provider(settings)


@pytest.mark.parametrize(
    "value",
    [
        "openai/model with spaces:1536",
        "openai/model!:1536",
        "ab",
        "x" * 129,
    ],
)
def test_profile_id_must_satisfy_the_schema_check_constraint(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    # Same pattern as vault_document_embeddings_profile_id_format. Failing at
    # startup beats failing on the first insert.
    monkeypatch.setenv("VAULT_EMBEDDING_PROFILE_ID", value)

    with pytest.raises(RuntimeError, match="VAULT_EMBEDDING_PROFILE_ID must match"):
        EmbeddingSettings.from_environment()


@pytest.mark.parametrize("value", ["has space", "/leading", "", "pro@vider"])
def test_provider_name_must_be_registry_key_shaped(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("VAULT_EMBEDDING_PROVIDER", value)

    with pytest.raises(RuntimeError, match="VAULT_EMBEDDING_PROVIDER must match"):
        EmbeddingSettings.from_environment()
