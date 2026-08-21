import shutil
import sys
from io import StringIO
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.vault.constants import (
    DEFAULT_TEXT_SEARCH_CONFIG as RUNTIME_DEFAULT_TEXT_SEARCH_CONFIG,
)
from app.vault.constants import (
    resolve_text_search_config as resolve_runtime_config,
)
from tests.vault.migration_helpers import (
    REPO_ROOT,
    migration_environment,
    run_leaderboard_migration,
    run_vault_migration,
)
from vault_migrations.helpers import (
    DEFAULT_TEXT_SEARCH_CONFIG as MIGRATION_DEFAULT_TEXT_SEARCH_CONFIG,
)
from vault_migrations.helpers import (
    resolve_text_search_config as resolve_migration_config,
)


VAULT_TABLES = {
    "vault_agent_credentials",
    "vault_audit_events",
    "vault_compile_runs",
    "vault_document_embeddings",
    "vault_documents",
    "vault_review_cases",
    "vault_write_requests",
}

LEADERBOARD_TABLES = {
    "auth_identities",
    "game_modes",
    "refresh_tokens",
    "runs",
    "scores",
    "submission_idempotency",
    "users",
}


def test_migration_and_runtime_text_search_defaults_match() -> None:
    assert MIGRATION_DEFAULT_TEXT_SEARCH_CONFIG == RUNTIME_DEFAULT_TEXT_SEARCH_CONFIG


@pytest.mark.parametrize("value", [None, "english", " simple "])
def test_migration_and_runtime_text_search_resolvers_accept_the_same_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("VAULT_TEXT_SEARCH_CONFIG", raising=False)
    else:
        monkeypatch.setenv("VAULT_TEXT_SEARCH_CONFIG", value)

    assert resolve_migration_config() == resolve_runtime_config()


@pytest.mark.parametrize(
    "value",
    ["English", "9english", "en-gb", "", "english;drop"],
)
def test_migration_and_runtime_text_search_resolvers_reject_the_same_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("VAULT_TEXT_SEARCH_CONFIG", value)

    with pytest.raises(RuntimeError) as migration_error:
        resolve_migration_config()
    with pytest.raises(RuntimeError) as runtime_error:
        resolve_runtime_config()

    assert str(migration_error.value) == str(runtime_error.value)


def table_names(database_url: str, schema: str) -> set[str]:
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = %s
            """,
            (schema,),
        ).fetchall()
    return {row[0] for row in rows}


def schema_exists(database_url: str, schema: str) -> bool:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s",
            (schema,),
        ).fetchone()
    return row is not None


def version(database_url: str, schema: str, table: str) -> str:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            f'SELECT version_num FROM "{schema}"."{table}"'
        ).fetchone()
    assert row is not None
    return row[0]


def vault_foreign_key_schemas(database_url: str) -> set[tuple[str, str]]:
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT source_ns.nspname, target_ns.nspname
            FROM pg_constraint constraint_row
            JOIN pg_class source_table
              ON source_table.oid = constraint_row.conrelid
            JOIN pg_namespace source_ns
              ON source_ns.oid = source_table.relnamespace
            JOIN pg_class target_table
              ON target_table.oid = constraint_row.confrelid
            JOIN pg_namespace target_ns
              ON target_ns.oid = target_table.relnamespace
            WHERE constraint_row.contype = 'f'
              AND source_ns.nspname = 'vault'
            """
        ).fetchall()
    return {(row[0], row[1]) for row in rows}


def vector_extension_version(database_url: str) -> str | None:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    return row[0] if row is not None else None


def test_complete_vault_lineage_renders_offline_sql() -> None:
    output = StringIO()
    config = Config(
        str(REPO_ROOT / "alembic-vault.ini"),
        output_buffer=output,
    )
    offline_url = "postgresql+psycopg://offline:offline@invalid/offline"

    with migration_environment(
        database_url_value=offline_url,
        vault_database_url_value=offline_url,
    ):
        command.upgrade(config, "head", sql=True)

    rendered = output.getvalue()
    assert "CREATE TABLE vault.vault_documents" in rendered
    assert "0011_review_candidate_optional" in rendered


def test_revision_graph_loads_from_extracted_migration_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical revisions must not depend on the staged ``app.vault`` path."""

    extracted_root = tmp_path / "extracted"
    extracted_migrations = extracted_root / "vault_migrations"
    shutil.copytree(REPO_ROOT / "vault_migrations", extracted_migrations)
    monkeypatch.syspath_prepend(str(extracted_root))
    for name in tuple(sys.modules):
        if name == "vault_migrations" or name.startswith("vault_migrations."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    config = Config()
    config.set_main_option("script_location", str(extracted_migrations))
    revisions = list(ScriptDirectory.from_config(config).walk_revisions())

    assert revisions[0].revision == "0011_review_candidate_optional"
    assert revisions[-1].revision == "0001_vault_foundation"


def test_shared_database_builds_independent_schema_lineages(
    disposable_database_urls: dict[str, str],
) -> None:
    shared_url = disposable_database_urls["shared"]

    run_leaderboard_migration(shared_url, "head")
    public_before_vault = table_names(shared_url, "public")
    assert LEADERBOARD_TABLES <= public_before_vault

    run_vault_migration(shared_url, "head")

    assert table_names(shared_url, "public") == public_before_vault
    assert table_names(shared_url, "vault") == {
        *VAULT_TABLES,
        "vault_alembic_version",
    }
    assert version(shared_url, "public", "alembic_version") == ("0004_auth_identities")
    assert version(shared_url, "vault", "vault_alembic_version") == (
        "0011_review_candidate_optional"
    )
    assert vault_foreign_key_schemas(shared_url) == {("vault", "vault")}
    assert vector_extension_version(shared_url) is not None

    run_vault_migration(shared_url, "base")

    assert table_names(shared_url, "public") == public_before_vault
    assert table_names(shared_url, "vault") == {"vault_alembic_version"}
    assert vector_extension_version(shared_url) is not None

    run_vault_migration(shared_url, "head")
    assert VAULT_TABLES <= table_names(shared_url, "vault")


def test_separate_databases_remain_configuration_only(
    disposable_database_urls: dict[str, str],
) -> None:
    leaderboard_url = disposable_database_urls["leaderboard"]
    vault_url = disposable_database_urls["vault"]

    run_leaderboard_migration(leaderboard_url, "head")
    run_vault_migration(vault_url, "head")

    assert LEADERBOARD_TABLES <= table_names(leaderboard_url, "public")
    assert schema_exists(leaderboard_url, "vault") is False

    assert table_names(vault_url, "public").isdisjoint(LEADERBOARD_TABLES)
    assert table_names(vault_url, "vault") == {
        *VAULT_TABLES,
        "vault_alembic_version",
    }
    assert version(vault_url, "vault", "vault_alembic_version") == (
        "0011_review_candidate_optional"
    )
    assert vector_extension_version(vault_url) is not None


def test_roll_forward_application_rollback_keeps_both_migration_graphs(
    disposable_database_urls: dict[str, str],
) -> None:
    """A recovery release may rerun both heads against an already-advanced DB."""

    shared_url = disposable_database_urls["shared"]
    run_leaderboard_migration(shared_url, "head")
    run_vault_migration(shared_url, "head")

    # This second pass represents the release phase of a roll-forward
    # application rollback: behavior may be reverted, but every applied
    # revision remains in its source tree and `upgrade head` is a no-op rather
    # than "can't locate revision".
    run_leaderboard_migration(shared_url, "head")
    run_vault_migration(shared_url, "head")

    assert version(shared_url, "public", "alembic_version") == (
        "0004_auth_identities"
    )
    assert version(shared_url, "vault", "vault_alembic_version") == (
        "0011_review_candidate_optional"
    )
