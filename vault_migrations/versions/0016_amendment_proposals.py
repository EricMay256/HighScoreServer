"""revision-bound amendment proposals

An ordinary agent may now propose a change without gaining the power to
overwrite an endorsed note. Proposals are workflow records, not documents: they
stay outside search, embeddings, deduplication, compilation and export until a
reviewer accepts the exact stored change. A proposal carries either a complete
replacement or a bounded unified diff against only the body. Diffs are
revalidated when applied, and removals require explicit reviewer acknowledgement.

``content_revision`` is the compare-and-swap token. It increments only when
caller-supplied document content changes; lifecycle judgements do not move it.
Every existing row starts at revision 1 because there was no proposal that
could have observed an earlier value.

``target_document_id`` is deliberately not a foreign key. The proposal and its
judgement are durable history and must survive retirement of the target, the
same reason audit target ids are correlations rather than references. An
acceptance against a missing target settles ``stale``.

``vault:propose`` is a distinct verb and is added to all three scope constraints.
This migration grants it to nobody directly; OAuth's application-level baseline
will include it for newly authorized clients.

Revision ID: 0016_amendment_proposals
Revises: 0015_note_compile_declined
Create Date: 2026-08-24
"""

from alembic import op


revision = "0016_amendment_proposals"
down_revision = "0015_note_compile_declined"
branch_labels = None
depends_on = None


_SCOPES = """ARRAY[
    'vault:read', 'vault:write', 'vault:propose', 'vault:update',
    'vault:delete', 'vault:review', 'vault:compile', 'vault:export'
]::text[]"""

_OLD_SCOPES = """ARRAY[
    'vault:read', 'vault:write', 'vault:update', 'vault:delete',
    'vault:review', 'vault:compile', 'vault:export'
]::text[]"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE vault.vault_amendment_proposal_state AS ENUM
            ('pending', 'accepted', 'rejected', 'stale');

        ALTER TABLE vault.vault_documents
            ADD COLUMN content_revision bigint NOT NULL DEFAULT 1,
            ADD CONSTRAINT vault_documents_content_revision_positive
                CHECK (content_revision > 0);

        CREATE TABLE vault.vault_amendment_proposals (
            id uuid PRIMARY KEY,
            target_document_id text NOT NULL,
            target_revision bigint NOT NULL,
            change_kind text NOT NULL,
            change jsonb NOT NULL,
            rationale text NOT NULL,
            state vault.vault_amendment_proposal_state NOT NULL DEFAULT 'pending',
            proposed_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            decided_at timestamptz,
            decided_by text,
            decision_note text,
            applied_revision bigint,
            removals_acknowledged boolean NOT NULL DEFAULT false,
            CONSTRAINT vault_amendment_proposals_target_nonempty
                CHECK (btrim(target_document_id) <> ''),
            CONSTRAINT vault_amendment_proposals_target_revision_positive
                CHECK (target_revision > 0),
            CONSTRAINT vault_amendment_proposals_change_kind_known
                CHECK (change_kind IN ('replacement', 'body_diff')),
            CONSTRAINT vault_amendment_proposals_change_object
                CHECK (jsonb_typeof(change) = 'object'),
            CONSTRAINT vault_amendment_proposals_change_shape CHECK (
                (change_kind = 'body_diff'
                    AND jsonb_typeof(change -> 'body_diff') = 'string'
                    AND change - 'body_diff' = '{}'::jsonb)
                OR (change_kind = 'replacement'
                    AND jsonb_typeof(change -> 'title') = 'string'
                    AND jsonb_typeof(change -> 'body') = 'string')
            ),
            CONSTRAINT vault_amendment_proposals_rationale_nonempty
                CHECK (btrim(rationale) <> ''),
            CONSTRAINT vault_amendment_proposals_proposer_nonempty
                CHECK (btrim(proposed_by) <> ''),
            CONSTRAINT vault_amendment_proposals_decision_consistent CHECK (
                (state = 'pending' AND decided_at IS NULL AND decided_by IS NULL
                    AND applied_revision IS NULL
                    AND removals_acknowledged = false)
                OR (state = 'accepted' AND decided_at IS NOT NULL
                    AND decided_by IS NOT NULL AND applied_revision IS NOT NULL)
                OR (state IN ('rejected', 'stale') AND decided_at IS NOT NULL
                    AND decided_by IS NOT NULL AND applied_revision IS NULL
                    AND removals_acknowledged = false)
            )
        );

        CREATE INDEX idx_vault_amendment_proposals_state_created
            ON vault.vault_amendment_proposals (state, created_at);
        """
    )
    for table, constraint in (
        ("vault_agent_credentials", "vault_agent_credentials_scopes_known"),
        ("vault_oauth_authorization_codes", "vault_oauth_codes_scopes_known"),
        ("vault_oauth_refresh_tokens", "vault_oauth_refresh_scopes_known"),
    ):
        op.execute(
            f"""
            ALTER TABLE vault.{table} DROP CONSTRAINT {constraint};
            ALTER TABLE vault.{table} ADD CONSTRAINT {constraint}
                CHECK (scopes <@ {_SCOPES});
            """
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE proposal_count bigint;
        BEGIN
            SELECT count(*) INTO proposal_count
            FROM vault.vault_amendment_proposals;
            IF proposal_count > 0 THEN
                RAISE EXCEPTION
                    'cannot drop amendment proposals: % proposal(s) carry '
                    'durable workflow history. Remove them deliberately before downgrading.',
                    proposal_count;
            END IF;
        END
        $$;
        """
    )
    for table, constraint in (
        ("vault_agent_credentials", "vault_agent_credentials_scopes_known"),
        ("vault_oauth_authorization_codes", "vault_oauth_codes_scopes_known"),
        ("vault_oauth_refresh_tokens", "vault_oauth_refresh_scopes_known"),
    ):
        op.execute(
            f"""
            UPDATE vault.{table}
            SET scopes = ARRAY(
                SELECT scope FROM unnest(scopes) AS scope
                WHERE scope <> 'vault:propose'
                ORDER BY scope
            )::text[]
            WHERE 'vault:propose' = ANY(scopes);
            ALTER TABLE vault.{table} DROP CONSTRAINT {constraint};
            ALTER TABLE vault.{table} ADD CONSTRAINT {constraint}
                CHECK (scopes <@ {_OLD_SCOPES});
            """
        )
    op.execute(
        """
        DROP TABLE vault.vault_amendment_proposals;
        DROP TYPE vault.vault_amendment_proposal_state;
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT vault_documents_content_revision_positive,
            DROP COLUMN content_revision;
        """
    )
