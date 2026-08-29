"""Allow amendment proposals that carry metadata only.

Adds `metadata` to the change-kind vocabulary and gives it a payload shape.
The kind exists because every existing edit path is a full replacement: adding
one edge to a note required resending its title and body, so a metadata change
was indistinguishable at the schema level from a content rewrite. See vault
ADR 0036.

The shape constraint enumerates the four permitted keys rather than leaving the
object open. That enumeration is the kind's entire safety claim -- these are the
fields that do not join `assemble_embedding_text`, so a proposal of this kind
cannot change what the note means to search. An unlisted key would be that claim
failing silently, which is exactly the failure mode a CHECK is good at refusing.

Revision ID: 0018_metadata_amendments
Revises: 0017_oauth_entitlements
"""

from alembic import op


revision = "0018_metadata_amendments"
down_revision = "0017_oauth_entitlements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_amendment_proposals
            DROP CONSTRAINT vault_amendment_proposals_change_kind_known,
            DROP CONSTRAINT vault_amendment_proposals_change_shape
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_amendment_proposals
            ADD CONSTRAINT vault_amendment_proposals_change_kind_known
                CHECK (change_kind IN ('replacement', 'body_diff', 'metadata')),
            ADD CONSTRAINT vault_amendment_proposals_change_shape CHECK (
                (change_kind = 'body_diff'
                 AND jsonb_typeof(change -> 'body_diff') = 'string'
                 AND change - 'body_diff' = '{}'::jsonb)
                OR
                (change_kind = 'replacement'
                 AND jsonb_typeof(change -> 'title') = 'string'
                 AND jsonb_typeof(change -> 'body') = 'string')
                OR
                (change_kind = 'metadata'
                 AND change <> '{}'::jsonb
                 AND change - 'related_ids' - 'source_ids' - 'facets'
                     - 'source_url' = '{}'::jsonb)
            )
        """
    )


def downgrade() -> None:
    # Every metadata proposal goes, whatever its state.
    #
    # An earlier version of this deleted only the pending ones, on the reasoning
    # that a decided proposal is history worth keeping. That reasoning was
    # self-contradictory: the constraint restored below has no `metadata` in its
    # vocabulary, so a surviving decided row makes `ADD CONSTRAINT` fail and the
    # downgrade impossible. There is nothing to keep such a row *as*.
    #
    # What is actually lost is small. A pending proposal is inert -- absent from
    # search and dedup, having changed no document -- so discarding it loses a
    # queued intention. A decided one has already been applied or refused, and
    # the decision itself survives in `vault_audit_events` under
    # `vault.amendment.review`, which this migration does not touch. The row
    # here is the request, not the record.
    op.execute(
        """
        DELETE FROM vault.vault_amendment_proposals
        WHERE change_kind = 'metadata'
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_amendment_proposals
            DROP CONSTRAINT vault_amendment_proposals_change_kind_known,
            DROP CONSTRAINT vault_amendment_proposals_change_shape
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_amendment_proposals
            ADD CONSTRAINT vault_amendment_proposals_change_kind_known
                CHECK (change_kind IN ('replacement', 'body_diff')),
            ADD CONSTRAINT vault_amendment_proposals_change_shape CHECK (
                (change_kind = 'body_diff'
                 AND jsonb_typeof(change -> 'body_diff') = 'string'
                 AND change - 'body_diff' = '{}'::jsonb)
                OR
                (change_kind = 'replacement'
                 AND jsonb_typeof(change -> 'title') = 'string'
                 AND jsonb_typeof(change -> 'body') = 'string')
            )
        """
    )
