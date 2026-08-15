import pytest

from app.vault.constants import (
    DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
    EMBEDDING_DIMENSIONS,
    ROUTER_TIMEOUT_BUDGET_SECONDS,
    embedding_retry_budget_seconds,
    max_embedding_timeout_seconds,
)
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


def test_a_configured_timeout_that_outlasts_the_router_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard has to be on the configured value, not only on the default.

    ``test_worst_case_retry_budget_fits_inside_the_router_timeout`` asserts the
    *constant* fits, which says nothing about what a deployment actually runs
    under. 10s shipped in `.env` and `.env.example` for months while that test
    passed: the timeout is per attempt, so 10 reads as a 10s ceiling and is
    really 38s.
    """

    monkeypatch.setenv("VAULT_EMBEDDING_TIMEOUT_SECONDS", "10")

    with pytest.raises(RuntimeError) as excinfo:
        EmbeddingSettings.from_environment()

    message = str(excinfo.value)
    # The arithmetic belongs in the message: an operator who set 10 needs to see
    # why 10 is not the number that matters, not just that it was rejected.
    assert "3 x 10s + 2 x 4s" in message
    assert "38s" in message
    assert "7.3s" in message
    assert "backfill" in message


def test_the_largest_fitting_timeout_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary is inclusive, so the advertised maximum actually works.

    Rejecting the exact value the error message tells you to use would be a
    small cruelty, and it is the sort of off-by-one nobody checks.
    """

    monkeypatch.setenv(
        "VAULT_EMBEDDING_TIMEOUT_SECONDS",
        str(max_embedding_timeout_seconds()),
    )

    settings = EmbeddingSettings.from_environment()

    assert (
        embedding_retry_budget_seconds(settings.timeout_seconds)
        <= ROUTER_TIMEOUT_BUDGET_SECONDS
    )


def test_the_shipped_default_leaves_room_for_the_rest_of_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """23s of 30s. The remainder covers auth, the search transaction, and slack."""

    monkeypatch.delenv("VAULT_EMBEDDING_TIMEOUT_SECONDS", raising=False)

    settings = EmbeddingSettings.from_environment()

    assert embedding_retry_budget_seconds(settings.timeout_seconds) == 23.0


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
