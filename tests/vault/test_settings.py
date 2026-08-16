import pytest

from app.vault.constants import (
    DEFAULT_TEXT_SEARCH_CONFIG,
    EMBEDDING_DIMENSIONS,
    resolve_text_search_config,
)
from app.vault.settings import EmbeddingSettings, VaultSettings


SHARED_URL = "postgresql+psycopg://user:secret@db.example.test:5432/hss"
SEPARATE_URL = "postgresql+psycopg://user:secret@db.example.test:5432/vault"


def make_settings(**overrides: object) -> VaultSettings:
    values: dict[str, object] = {
        "enabled": True,
        "database_url": SHARED_URL,
        "hss_database_url": SHARED_URL,
        "pool_size": 2,
        "pool_timeout_seconds": 5,
        "hss_pool_max_size": 4,
        "process_count": 2,
        "hss_connection_limit": 20,
        "vault_connection_limit": 20,
        "operational_connection_reserve": 2,
        "text_search_config": "english",
    }
    values.update(overrides)
    return VaultSettings(**values)


def test_essential_zero_shared_budget_leaves_thirty_percent() -> None:
    budget = make_settings().validate_connection_budget()

    assert budget.shared_database is True
    assert budget.hss_allocated == 8
    assert budget.vault_allocated == 4
    assert budget.combined_allocated == 14
    assert budget.hss_limit - budget.combined_allocated == 6


def test_prior_hss_pool_size_fails_shared_budget() -> None:
    settings = make_settings(hss_pool_max_size=10)

    with pytest.raises(RuntimeError, match="connection budget exceeded"):
        settings.validate_connection_budget()


def test_hss_pool_of_five_no_longer_fits_beside_the_vault() -> None:
    """The vault's second connection is what cost HSS its fifth.

    5 * 2 + 2 * 2 + 2 = 16, one over the 15 left after the 25% reserve. This is
    the check catching a half-applied config change -- raising
    VAULT_DB_POOL_SIZE without lowering HSS_DB_POOL_MAX_SIZE -- which fails at
    lifespan and would take the leaderboard down with the vault.
    """

    settings = make_settings(hss_pool_max_size=5)

    with pytest.raises(RuntimeError, match="connection budget exceeded"):
        settings.validate_connection_budget()


def test_separate_database_budgets_are_calculated_independently() -> None:
    settings = make_settings(database_url=SEPARATE_URL)

    budget = settings.validate_connection_budget()

    assert budget.shared_database is False
    assert budget.hss_allocated == 10
    assert budget.vault_allocated == 6
    assert budget.combined_allocated is None


def test_database_identity_ignores_credentials() -> None:
    settings = make_settings(
        database_url="postgresql+psycopg://vault:other@db.example.test:5432/hss"
    )

    assert settings.shared_database is True


def test_vault_budget_rejects_undersized_separate_plan() -> None:
    settings = make_settings(
        database_url=SEPARATE_URL,
        pool_size=3,
        vault_connection_limit=10,
    )

    with pytest.raises(RuntimeError, match="Vault database"):
        settings.validate_connection_budget()


def test_embedding_dimensions_default_to_schema_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAULT_EMBEDDING_DIMENSIONS", raising=False)

    settings = EmbeddingSettings.from_environment()

    assert settings.embedding_dimensions == EMBEDDING_DIMENSIONS
    assert settings.profile_id.endswith(f":{EMBEDDING_DIMENSIONS}")


def test_embedding_dimensions_cannot_drift_from_migrated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_EMBEDDING_DIMENSIONS", "1024")

    with pytest.raises(RuntimeError, match="requires an Alembic migration"):
        EmbeddingSettings.from_environment()


def test_text_search_config_defaults_to_english_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAULT_TEXT_SEARCH_CONFIG", raising=False)

    assert resolve_text_search_config() == "english"
    assert DEFAULT_TEXT_SEARCH_CONFIG == "english"


def test_text_search_config_accepts_another_catalog_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_TEXT_SEARCH_CONFIG", "simple")

    assert resolve_text_search_config() == "simple"


@pytest.mark.parametrize(
    "value",
    [
        "english'); DROP TABLE vault.vault_documents; --",
        "English",
        "9english",
        "en-gb",
        "",
    ],
)
def test_text_search_config_rejects_values_unsafe_for_ddl(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    # The name is interpolated into a generated-column expression, so anything
    # that is not identifier-shaped is refused before it can reach SQL.
    monkeypatch.setenv("VAULT_TEXT_SEARCH_CONFIG", value)

    with pytest.raises(RuntimeError, match="VAULT_TEXT_SEARCH_CONFIG must match"):
        resolve_text_search_config()
