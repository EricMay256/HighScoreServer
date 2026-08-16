"""split vault:write into contribute, update, and delete

``vault:write`` gated all three write routes: contribute, replace, and retire.
So "may add a note" and "may permanently destroy one" were the same grant, and
the `importer` credential -- the only long-lived one, and one that needs to
contribute and replace but never to delete -- held it.

The tight retire quota (10/min burst 5, deliberately the tightest bucket in
`LIMITS`, on the reasoning that a loop that deletes is worse than a loop that
writes) already expressed the judgement that deletion is the dangerous verb. It
expressed it in the rate limiter, which bounds how *fast* a credential may
destroy things rather than *whether* it may. This moves the judgement to the
authorization layer, where a decision about what a principal may do belongs.

Scopes are verbs (ADR 0015), so this refines the existing model rather than
introducing a new one: `vault:write` narrows to contribute, `vault:update`
covers replacement, `vault:delete` covers retirement.

**This migration changes the schema only. It grants nothing.**

An earlier draft granted the two new scopes to every existing `vault:write`
holder, so that clients issued before the split kept working. That is a
reasonable one-time intent and the wrong thing to put in a migration: a
migration is a procedure that reruns. Rebuilding a staging database, testing a
revision, or rolling back and re-deploying would each silently re-grant
permissions an operator had deliberately removed, with nothing in the logs
saying a privilege had been restored. A data migration that re-applies privilege
on every run is the wrong shape no matter who runs it, and this one was carrying
that risk for three rows on one developer machine -- production has never
deployed the vault and holds no vault credentials at all.

Widening existing credentials is therefore a **manual, audited, one-time**
operation. It is not idempotent-by-rerun, because it should not be:

    UPDATE vault.vault_agent_credentials
    SET scopes = (
        SELECT array_agg(scope ORDER BY scope)
        FROM (SELECT unnest(scopes || ARRAY['vault:update', 'vault:delete']::text[]) AS scope) w
    )
    WHERE id = '<credential-id>';

Per credential, deliberately, after deciding that credential actually needs the
verb. The alternative -- and the better one for anything long-lived -- is to
reissue with exactly the scopes it needs, which `issue_vault_credential.py` has
always supported.

Revision ID: 0007_write_scope_split
Revises: 0006_request_digest_version
Create Date: 2026-08-15
"""

from alembic import op


revision = "0007_write_scope_split"
down_revision = "0006_request_digest_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_agent_credentials
            DROP CONSTRAINT vault_agent_credentials_scopes_known;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_agent_credentials
            ADD CONSTRAINT vault_agent_credentials_scopes_known
            CHECK (scopes <@ ARRAY[
                'vault:read',
                'vault:write',
                'vault:update',
                'vault:delete',
                'vault:review',
                'vault:compile',
                'vault:export'
            ]::text[]);
        """
    )


def downgrade() -> None:
    # Stripping the new scopes is constraint satisfaction, not an authorization
    # decision: the narrowed CHECK below rejects any row still carrying them, so
    # without this the ALTER fails partway through.
    #
    # The asymmetry with upgrade() is deliberate and is the safe direction. A
    # downgrade followed by an upgrade *loses* vault:update and vault:delete and
    # does not restore them, so the cycle can only ever reduce what a credential
    # may do. Re-granting is the manual step in this module's docstring, taken
    # per credential by someone who decided to take it.
    op.execute(
        """
        UPDATE vault.vault_agent_credentials
        SET scopes = (
            SELECT coalesce(array_agg(scope ORDER BY scope), ARRAY[]::text[])
            FROM unnest(scopes) AS scope
            WHERE scope NOT IN ('vault:update', 'vault:delete')
        )
        WHERE scopes && ARRAY['vault:update', 'vault:delete']::text[];
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_agent_credentials
            DROP CONSTRAINT vault_agent_credentials_scopes_known;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_agent_credentials
            ADD CONSTRAINT vault_agent_credentials_scopes_known
            CHECK (scopes <@ ARRAY[
                'vault:read',
                'vault:write',
                'vault:review',
                'vault:compile',
                'vault:export'
            ]::text[]);
        """
    )
