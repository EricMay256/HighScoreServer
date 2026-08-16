"""document vault_path and doc_status

Adds the two columns the importer needs to round-trip a governed note.

``vault_path`` is the vault-root-relative posix path, extension included --
exactly the ``rel_path`` the governance engine matches folder rules against. It
is the key that ties a row to its file, so it is NOT NULL and UNIQUE. See vault
ADR 0010.

``doc_status`` carries the Status Map value from ``types.yml``, which the
existing ``status`` column cannot represent: a Wiki Page is Current or Stub, an
Agent Note is Active or Flagged, and ``status`` is
``('active','flagged','archived')``. ``status`` stays the read surface's
visibility gate that ADR 0008 depends on. See vault ADR 0011.

Both new columns constrain shape only, never vocabulary, for the reason given
in ADR 0009: ``types.yml`` and ``folders.yml`` are meant to evolve without a
migration.

Backfill note: every row that can exist when this runs is an agent note --
the write path is unbuilt, the vault has never been enabled in production, and
the only writers are the test fixtures and the demo seeder. ``Agent/notes/<id>.md``
is therefore those rows' correct path rather than an invented one. If a later
import claims the same path, the UNIQUE constraint refuses it, which is the
right outcome.

Revision ID: 0003_vault_path_doc_status
Revises: 0002_document_doc_type
Create Date: 2026-07-29
"""

from alembic import op


revision = "0003_vault_path_doc_status"
down_revision = "0002_document_doc_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN vault_path TEXT,
            ADD COLUMN doc_status TEXT;

        UPDATE vault.vault_documents
            SET vault_path = 'Agent/notes/' || id || '.md'
            WHERE vault_path IS NULL;

        ALTER TABLE vault.vault_documents
            ALTER COLUMN vault_path SET NOT NULL;

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_vault_path_key UNIQUE (vault_path);

        -- Vault-root-relative posix, extension included: no leading or trailing
        -- slash, no empty segment, no '.' or '..' segment, no backslash. Shape
        -- only -- which folders exist is folders.yml's business.
        --
        -- '[.]' rather than an escaped dot, and chr(92) rather than a literal
        -- backslash: PostgreSQL's advanced regular expressions treat a
        -- backslash as special inside a bracket expression, so spelling either
        -- one directly is a portability trap rather than a stricter check.
        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_vault_path_format
            CHECK (
                btrim(vault_path) <> ''
                AND vault_path !~ '^/'
                AND vault_path !~ '/$'
                AND vault_path !~ '//'
                AND vault_path !~ '(^|/)[.][.]?(/|$)'
                AND strpos(vault_path, chr(92)) = 0
                AND length(vault_path) <= 1024
            );

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_doc_status_format
            CHECK (
                doc_status IS NULL
                OR doc_status ~ '^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$'
            );

        -- Every folders.yml glob is a literal prefix followed by '/**', so
        -- policy resolution is a longest-prefix match. The UNIQUE index above
        -- uses the default collation, which cannot serve LIKE 'prefix%' unless
        -- the database is C-collated; text_pattern_ops can, always.
        CREATE INDEX idx_vault_documents_vault_path_prefix
            ON vault.vault_documents
            USING btree (vault_path text_pattern_ops);
        """
    )


def downgrade() -> None:
    # Local/test rollback only. Dropping the columns discards paths and
    # statuses; there is no lossless reverse for this revision.
    op.execute(
        """
        DROP INDEX IF EXISTS vault.idx_vault_documents_vault_path_prefix;

        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_doc_status_format;
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_vault_path_format;
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_vault_path_key;

        ALTER TABLE vault.vault_documents
            DROP COLUMN IF EXISTS doc_status,
            DROP COLUMN IF EXISTS vault_path;
        """
    )
