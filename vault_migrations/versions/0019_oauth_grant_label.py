"""An operator-assigned label on one OAuth authorization.

Principals read as `oauth-<uuid4>` and stay that way: the id is derived from
the registration rather than the client's declared name, so two separately
approved clients cannot collide into one quota and one idempotency namespace.
The label is display only, added beside that id rather than in place of it --
it never resolves a credential, keys a quota, or names a principal in an audit
record. See vault ADR 0040.

Nullable with no backfill. An unlabelled authorization is the ordinary state,
not a deficiency, and every existing row keeps behaving exactly as it did.

The shape constraint refuses an empty label and caps its length. Clearing is
`NULL`, so a blank string would be a second spelling of absent that every
reader would have to know about; the cap is there because the value is
unverified operator text that reaches a browser header and a fixed-width column
in `issue_vault_credential list`.

Revision ID: 0019_oauth_grant_label
Revises: 0018_metadata_amendments
Create Date: 2026-08-31
"""

from alembic import op


revision = "0019_oauth_grant_label"
down_revision = "0018_metadata_amendments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_oauth_grants
            ADD COLUMN label text,
            ADD CONSTRAINT vault_oauth_grants_label_shape
                CHECK (
                    label IS NULL
                    OR (btrim(label) <> '' AND length(label) <= 120)
                )
        """
    )


def downgrade() -> None:
    # The constraint goes with the column; naming it separately would fail on
    # the second run of a downgrade that got half way.
    op.execute(
        """
        ALTER TABLE vault.vault_oauth_grants
            DROP COLUMN label
        """
    )
