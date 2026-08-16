"""document doc_type

Adds the governance Type Dictionary discriminator to vault documents. ``kind``
stays the coarse storage and lifecycle discriminator it has always been, and
keeps its role in the compile-provenance constraint; ``doc_type`` carries the
type vocabulary, which is defined in ``types.yml`` and is meant to evolve
without a migration. See vault ADR 0009.

The column is nullable because "not yet typed" is a real state: wiki-layer
documents are compiled rather than authored and may carry no Type Dictionary
type at all, and a NOT NULL column would have to be backfilled with an invented
default. Absence of a value means untyped, in the same way that absence of a
row in ``vault_document_embeddings`` means unembedded.

The CHECK here constrains shape only — non-blank, bounded, no control
characters — and deliberately not vocabulary. Which names are legal is
``types.yml``'s business, validated in application code, precisely so that
adding a type is not a migration.

Revision ID: 0002_document_doc_type
Revises: 0001_vault_foundation
Create Date: 2026-07-29
"""

from alembic import op


revision = "0002_document_doc_type"
down_revision = "0001_vault_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN doc_type TEXT;

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_doc_type_format
            CHECK (
                doc_type IS NULL
                OR doc_type ~ '^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$'
            );
        """
    )


def downgrade() -> None:
    # Local/test rollback only. Dropping the column discards any assigned
    # types; there is no lossless reverse for this revision.
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_doc_type_format;
        ALTER TABLE vault.vault_documents
            DROP COLUMN IF EXISTS doc_type;
        """
    )
