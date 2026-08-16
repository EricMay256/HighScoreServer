"""preserve compile runs referenced by durable wiki provenance

Wiki documents require ``compile_run_id``, ``compiled_by``, and ``compiled_at``
as one provenance unit. The original ``ON DELETE SET NULL`` action attempted to
clear only the run id, which the provenance CHECK then rejected. Compile runs
are durable provenance, so referenced rows must not be deleted.

Revision ID: 0008_compile_run_restrict
Revises: 0007_write_scope_split
Create Date: 2026-08-16
"""

from alembic import op


revision = "0008_compile_run_restrict"
down_revision = "0007_write_scope_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT vault_documents_compile_run_id_fkey;
        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_compile_run_id_fkey
            FOREIGN KEY (compile_run_id)
            REFERENCES vault.vault_compile_runs(id)
            ON DELETE RESTRICT;
        """
    )


def downgrade() -> None:
    # Restores the historical schema for local migration-cycle tests. Deleting
    # a referenced run under this action remains invalid because the provenance
    # CHECK rejects the resulting partial provenance.
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT vault_documents_compile_run_id_fkey;
        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_compile_run_id_fkey
            FOREIGN KEY (compile_run_id)
            REFERENCES vault.vault_compile_runs(id)
            ON DELETE SET NULL;
        """
    )
