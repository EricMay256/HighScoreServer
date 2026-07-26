import pytest

from app.vault.settings import VaultSettings


SHARED_URL = "postgresql+psycopg://user:secret@db.example.test:5432/hss"
SEPARATE_URL = "postgresql+psycopg://user:secret@db.example.test:5432/vault"


def make_settings(**overrides: object) -> VaultSettings:
    values: dict[str, object] = {
        "enabled": True,
        "database_url": SHARED_URL,
        "hss_database_url": SHARED_URL,
        "pool_size": 1,
        "pool_timeout_seconds": 5,
        "hss_pool_max_size": 5,
        "process_count": 2,
        "hss_connection_limit": 20,
        "vault_connection_limit": 20,
        "operational_connection_reserve": 2,
    }
    values.update(overrides)
    return VaultSettings(**values)


def test_essential_zero_shared_budget_leaves_thirty_percent() -> None:
    budget = make_settings().validate_connection_budget()

    assert budget.shared_database is True
    assert budget.hss_allocated == 10
    assert budget.vault_allocated == 2
    assert budget.combined_allocated == 14
    assert budget.hss_limit - budget.combined_allocated == 6


def test_prior_hss_pool_size_fails_shared_budget() -> None:
    settings = make_settings(hss_pool_max_size=10)

    with pytest.raises(RuntimeError, match="connection budget exceeded"):
        settings.validate_connection_budget()


def test_separate_database_budgets_are_calculated_independently() -> None:
    settings = make_settings(database_url=SEPARATE_URL)

    budget = settings.validate_connection_budget()

    assert budget.shared_database is False
    assert budget.hss_allocated == 12
    assert budget.vault_allocated == 4
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
