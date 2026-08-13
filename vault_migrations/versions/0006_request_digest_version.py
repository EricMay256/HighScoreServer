"""request digest version

``vault_write_requests.request_sha256`` answers "is this the same request as the
one that used this key before". Until now it was compared without recording
*how* it was computed, which made the comparison silently depend on the server's
schema version rather than on the caller's payload.

The digest hashed the validated Pydantic model with ``exclude_none=False``, so
it covered every field the model declared -- including ones the client never
sent, serialized at their defaults. Adding ``summary``, ``aliases``, ``facets``,
``related_ids`` and ``source_ids`` in ``5bdd5ad`` therefore changed the digest of
every possible request. On 2026-08-13 the corpus importer replayed 48 unchanged
notes and all 39 previously-imported ones came back 409: identical bytes on the
wire, a different digest on the server. Nothing had drifted except the schema.

That is a general defect, not a one-off: under the old rule *any* additive,
backward-compatible field addition invalidates every idempotency record in the
table, and the failure surfaces as a conflict blaming the client.

Two changes together fix it. The digest now covers only the fields the client
actually supplied (``exclude_unset=True``), which is stable across additive
schema change; and this column records which rule produced a stored digest, so
rows written under the old rule are recognisable rather than merely wrong.

Existing rows default to 1. They are not recomputable -- the original payloads
were never stored, only their digests -- so the service treats a version
mismatch as "cannot verify" and replays without comparing, rather than raising a
conflict it has no evidence for. That is a deliberate one-time weakening of
conflict detection for the 48 rows that predate this migration; keys written
from here on compare exactly.

Revision ID: 0006_request_digest_version
Revises: 0005_document_facets
Create Date: 2026-08-13
"""

from alembic import op


revision = "0006_request_digest_version"
down_revision = "0005_document_facets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_write_requests
            ADD COLUMN digest_version SMALLINT NOT NULL DEFAULT 1;
        """
    )

    # A version is an identity, not a measurement: zero and negatives are not
    # "unknown", they are impossible, and the check keeps a miswritten client
    # from inventing a rule the service will later try to honour.
    op.execute(
        """
        ALTER TABLE vault.vault_write_requests
            ADD CONSTRAINT vault_write_requests_digest_version_positive
            CHECK (digest_version > 0);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_write_requests
            DROP CONSTRAINT IF EXISTS vault_write_requests_digest_version_positive;
        ALTER TABLE vault.vault_write_requests
            DROP COLUMN IF EXISTS digest_version;
        """
    )
