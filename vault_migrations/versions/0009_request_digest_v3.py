"""record recursive request-digest canonicalization

Digest v3 recursively sorts JSON object keys so semantically identical nested
objects do not conflict only because their insertion order differs. Existing
rows keep their original version and use ADR 0016's one-replay compatibility
path; only the default for newly inserted rows changes. Application writes
already supply the version explicitly, while the database default keeps direct
or future writers from silently minting a retired v1 digest.

Revision ID: 0009_request_digest_v3
Revises: 0008_compile_run_restrict
Create Date: 2026-08-16
"""

from alembic import op


revision = "0009_request_digest_v3"
down_revision = "0008_compile_run_restrict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_write_requests
            ALTER COLUMN digest_version SET DEFAULT 3;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_write_requests
            ALTER COLUMN digest_version SET DEFAULT 1;
        """
    )
