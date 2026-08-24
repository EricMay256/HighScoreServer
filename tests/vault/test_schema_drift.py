import os

import psycopg
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect

from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.settings import normalize_sqlalchemy_url
from app.vault.tables import TEXT_SEARCH_CONFIG, metadata


def test_core_metadata_matches_migrated_vault_schema(
    configure_test_env: None,
) -> None:
    engine = create_engine(normalize_sqlalchemy_url(os.environ["DATABASE_URL"]))
    inspector = inspect(engine)
    try:
        expected_table_names = {table.name for table in metadata.tables.values()}
        actual_table_names = set(inspector.get_table_names(schema="vault"))
        assert actual_table_names == {
            *expected_table_names,
            "vault_alembic_version",
        }

        for table in metadata.tables.values():
            actual_columns = {
                column["name"]: column
                for column in inspector.get_columns(
                    table.name,
                    schema="vault",
                )
            }
            assert set(actual_columns) == {column.name for column in table.columns}

            for column in table.columns:
                actual = actual_columns[column.name]
                assert actual["nullable"] is column.nullable
                if column.identity is not None:
                    assert actual.get("identity") is not None
                elif column.computed is not None:
                    assert actual.get("computed") is not None
                elif column.server_default is not None:
                    assert actual.get("default") is not None

            actual_primary_key = inspector.get_pk_constraint(
                table.name,
                schema="vault",
            )
            assert set(actual_primary_key["constrained_columns"]) == {
                column.name for column in table.primary_key.columns
            }

            expected_unique_columns = {
                tuple(constraint.columns.keys())
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            actual_unique_columns = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(
                    table.name,
                    schema="vault",
                )
            }
            assert actual_unique_columns == expected_unique_columns

            expected_check_names = {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            actual_check_names = {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    table.name,
                    schema="vault",
                )
            }
            assert actual_check_names == expected_check_names

            expected_foreign_keys = {
                (
                    tuple(constraint.column_keys),
                    tuple(element.column.name for element in constraint.elements),
                    "vault",
                )
                for constraint in table.foreign_key_constraints
            }
            actual_foreign_keys = {
                (
                    tuple(constraint["constrained_columns"]),
                    tuple(constraint["referred_columns"]),
                    constraint["referred_schema"],
                )
                for constraint in inspector.get_foreign_keys(
                    table.name,
                    schema="vault",
                )
            }
            assert actual_foreign_keys == expected_foreign_keys

            expected_indexes = {index.name for index in table.indexes}
            actual_indexes = {
                index["name"]
                for index in inspector.get_indexes(
                    table.name,
                    schema="vault",
                )
                if not index.get("duplicates_constraint")
            }
            assert actual_indexes == expected_indexes
    finally:
        engine.dispose()


def test_postgresql_specific_vault_ddl_matches_contract(
    configure_test_env: None,
) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        vector_row = connection.execute(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute attribute
            JOIN pg_class table_row ON table_row.oid = attribute.attrelid
            JOIN pg_namespace schema_row
              ON schema_row.oid = table_row.relnamespace
            WHERE schema_row.nspname = 'vault'
              AND table_row.relname = 'vault_document_embeddings'
              AND attribute.attname = 'embedding'
            """
        ).fetchone()
        assert vector_row == (f"vector({EMBEDDING_DIMENSIONS})",)

        generated_row = connection.execute(
            """
            SELECT
                attribute.attgenerated,
                pg_get_expr(default_row.adbin, default_row.adrelid)
            FROM pg_attribute attribute
            JOIN pg_class table_row ON table_row.oid = attribute.attrelid
            JOIN pg_namespace schema_row
              ON schema_row.oid = table_row.relnamespace
            JOIN pg_attrdef default_row
              ON default_row.adrelid = attribute.attrelid
             AND default_row.adnum = attribute.attnum
            WHERE schema_row.nspname = 'vault'
              AND table_row.relname = 'vault_documents'
              AND attribute.attname = 'search_vector'
            """
        ).fetchone()
        assert generated_row is not None
        assert generated_row[0] == "s"
        assert "setweight" in generated_row[1]
        assert "title" in generated_row[1]
        assert "summary" in generated_row[1]
        assert "body" in generated_row[1]
        # The configuration is baked in at migration time; CI pins the variable
        # so this compares against a fixed target rather than the environment.
        assert f"'{TEXT_SEARCH_CONFIG}'::regconfig" in generated_row[1]

        index_row = connection.execute(
            """
            SELECT
                access_method.amname,
                pg_get_indexdef(index_row.indexrelid),
                pg_get_expr(index_row.indpred, index_row.indrelid)
            FROM pg_index index_row
            JOIN pg_class index_class
              ON index_class.oid = index_row.indexrelid
            JOIN pg_am access_method
              ON access_method.oid = index_class.relam
            WHERE index_class.relname = 'idx_vault_document_embeddings_hnsw'
            """
        ).fetchone()
        assert index_row is not None
        assert index_row[0] == "hnsw"
        assert "vector_cosine_ops" in index_row[1]
        # embedding is NOT NULL on the join table, so the index covers every row
        # and carries no partial predicate.
        assert index_row[2] is None

        enum_rows = connection.execute(
            """
            SELECT type_row.typname, enum_row.enumlabel
            FROM pg_type type_row
            JOIN pg_namespace schema_row
              ON schema_row.oid = type_row.typnamespace
            JOIN pg_enum enum_row ON enum_row.enumtypid = type_row.oid
            WHERE schema_row.nspname = 'vault'
            ORDER BY type_row.typname, enum_row.enumsortorder
            """
        ).fetchall()
        enum_values: dict[str, list[str]] = {}
        for enum_name, enum_value in enum_rows:
            enum_values.setdefault(enum_name, []).append(enum_value)
        assert enum_values == {
            "vault_compile_run_state": ["running", "succeeded", "failed"],
            "vault_document_kind": ["note", "wiki"],
            "vault_document_status": ["active", "flagged", "archived"],
            "vault_promotion_status": ["candidate", "promoted", "retracted"],
            "vault_review_state": [
                "pending",
                "accepted",
                "rejected",
                "superseded",
            ],
            "vault_write_request_state": [
                "processing",
                "inserted",
                "flagged",
                "invalid",
                "failed",
            ],
        }

        extension_row = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        assert extension_row is not None

        version_table_row = connection.execute(
            """
            SELECT schema_row.nspname
            FROM pg_class table_row
            JOIN pg_namespace schema_row
              ON schema_row.oid = table_row.relnamespace
            WHERE table_row.relname = 'vault_alembic_version'
              AND table_row.relkind = 'r'
            """
        ).fetchone()
        assert version_table_row == ("vault",)

        cross_schema_foreign_key = connection.execute(
            """
            SELECT 1
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
              AND target_ns.nspname <> 'vault'
            LIMIT 1
            """
        ).fetchone()
        assert cross_schema_foreign_key is None
