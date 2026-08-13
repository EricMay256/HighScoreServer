import psycopg

from tests.vault.migration_helpers import (
    run_leaderboard_migration,
    run_vault_migration,
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
        "0005_document_facets"
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
        "0005_document_facets"
    )
    assert vector_extension_version(vault_url) is not None
