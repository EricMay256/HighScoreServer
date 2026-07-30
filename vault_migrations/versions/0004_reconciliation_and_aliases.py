"""reconciliation hashes, aliases, and frontmatter

Everything the Markdown importer needs that the schema did not yet carry.

``vault_documents.source_sha256`` is the hash of the upstream file, and drives
mark-and-sweep reconciliation (vault ADR 0012). NULL means the row has no
upstream file -- it was authored in the database -- so the column doubles as a
per-row statement of which direction truth flows for that document.

``vault_document_embeddings.embedded_text_sha256`` is the hash of the text that
produced *that* vector, so a change to bookkeeping frontmatter does not buy an
embedding call while a change to an alias does (vault ADR 0013). It lives on
the embedding rather than the document because staleness is per profile.

``aliases`` and ``frontmatter`` exist because the projector must re-emit notes
the validator accepts. Aliases are alternative titles, so they join
``search_vector`` at weight A; ``frontmatter`` is the JSONB bag for keys the
schema does not model, which is unavoidable given global.yml's known_extra_keys.

Tags are deliberately NOT added to search_vector. They are a controlled
vocabulary already served by a GIN index for exact filtering, and fuzzy-matching
what you can filter exactly is a downgrade. They do join the embedding text,
which is a different job.

Revision ID: 0004_reconciliation
Revises: 0003_vault_path_doc_status
Create Date: 2026-07-29
"""

import sqlalchemy as sa
from alembic import op

from app.vault.constants import resolve_text_search_config


revision = "0004_reconciliation"
down_revision = "0003_vault_path_doc_status"
branch_labels = None
depends_on = None


def _text_search_config() -> str:
    """Resolve and catalog-validate the configuration, as revision 0001 does.

    The name is interpolated into DDL, so it is checked against the live
    catalog rather than trusted.
    """

    config = resolve_text_search_config()
    existing = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM pg_catalog.pg_ts_config WHERE cfgname = :name"),
            {"name": config},
        )
        .scalar()
    )
    if existing is None:
        raise RuntimeError(
            f"VAULT_TEXT_SEARCH_CONFIG={config!r} is not a text search "
            "configuration in this database."
        )
    return config


def upgrade() -> None:
    config = _text_search_config()

    # array_to_string is STABLE, not IMMUTABLE, so it cannot appear in a
    # generated column -- PostgreSQL rejects the DDL outright. The volatility
    # marking is generic conservatism about element output functions; for
    # text[] specifically the result depends only on the array contents and the
    # separator. This wrapper is pinned to text[] so it cannot be applied to a
    # type where that reasoning fails.
    #
    # array_to_tsvector is IMMUTABLE and was rejected instead: it emits raw
    # lexemes ('Postgres'), which never match a stemmed query side ('postgr'),
    # so aliases indexed that way would be silently unsearchable.
    op.execute(
        """
        CREATE FUNCTION vault.text_array_to_string(text[], text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$ SELECT array_to_string($1, $2) $$;
        """
    )

    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN source_sha256 BYTEA,
            ADD COLUMN aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
            ADD COLUMN frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb;

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_source_sha256_length
            CHECK (source_sha256 IS NULL OR octet_length(source_sha256) = 32);

        ALTER TABLE vault.vault_document_embeddings
            ADD COLUMN embedded_text_sha256 BYTEA;

        ALTER TABLE vault.vault_document_embeddings
            ADD CONSTRAINT vault_document_embeddings_text_sha256_length
            CHECK (
                embedded_text_sha256 IS NULL
                OR octet_length(embedded_text_sha256) = 32
            );
        """
    )

    # Dropping the generated column drops its GIN index with it, so both are
    # rebuilt. This rewrites the table; it is cheap now and would not be later.
    op.execute(
        f"""
        DROP INDEX vault.idx_vault_documents_search_vector;
        ALTER TABLE vault.vault_documents DROP COLUMN search_vector;

        ALTER TABLE vault.vault_documents
            ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(title, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector(
                        '{config}'::regconfig,
                        coalesce(vault.text_array_to_string(aliases, ' '), '')
                    ),
                    'A'
                ) ||
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(summary, '')),
                    'B'
                ) ||
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(body, '')),
                    'C'
                )
            ) STORED NOT NULL;

        CREATE INDEX idx_vault_documents_search_vector
            ON vault.vault_documents USING gin (search_vector);
        """
    )


def downgrade() -> None:
    # Local/test rollback only. Restores the pre-alias generated column, then
    # drops the new columns and the wrapper function.
    config = _text_search_config()
    op.execute(
        f"""
        DROP INDEX IF EXISTS vault.idx_vault_documents_search_vector;
        ALTER TABLE vault.vault_documents DROP COLUMN IF EXISTS search_vector;

        ALTER TABLE vault.vault_documents
            ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(title, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(summary, '')),
                    'B'
                ) ||
                setweight(
                    to_tsvector('{config}'::regconfig, coalesce(body, '')),
                    'C'
                )
            ) STORED NOT NULL;

        CREATE INDEX idx_vault_documents_search_vector
            ON vault.vault_documents USING gin (search_vector);

        ALTER TABLE vault.vault_document_embeddings
            DROP CONSTRAINT IF EXISTS
                vault_document_embeddings_text_sha256_length;
        ALTER TABLE vault.vault_document_embeddings
            DROP COLUMN IF EXISTS embedded_text_sha256;

        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_source_sha256_length;
        ALTER TABLE vault.vault_documents
            DROP COLUMN IF EXISTS frontmatter,
            DROP COLUMN IF EXISTS aliases,
            DROP COLUMN IF EXISTS source_sha256;

        DROP FUNCTION IF EXISTS vault.text_array_to_string(text[], text);
        """
    )
