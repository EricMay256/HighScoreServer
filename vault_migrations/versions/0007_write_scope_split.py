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

**Existing holders are grandfathered, and that is deliberate.** Narrowing
`vault:write` without granting the new scopes would silently strip replace and
delete from every credential already issued -- discovered at the next run of a
working client, as a 403 that names no cause. Least privilege is a property
worth having for credentials issued from here on; retroactively revoking
capability from the one client that exists buys nothing and breaks the corpus
importer. Operators who want the narrower grant reissue, which is a two-command
operation and now actually expressible.

The grandfather clause is scoped to rows that hold `vault:write` *at this
moment*. It is not a trigger and not a default: a credential issued after this
migration gets exactly the scopes it was issued with.

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
    # Widen before granting: the grant writes values the old constraint rejects,
    # so the reverse order fails on the first row.
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

    # Grandfather: anything that could write before can still update and delete.
    #
    # Idempotent by construction -- a row that already carries the scope is
    # unchanged by the union, and the WHERE keeps the write off rows that would
    # not change. Sorted so the stored order stays stable, which keeps
    # `issue_vault_credential list` output diffable.
    op.execute(
        """
        UPDATE vault.vault_agent_credentials
        SET scopes = (
            SELECT array_agg(scope ORDER BY scope)
            FROM (
                SELECT unnest(
                    scopes || ARRAY['vault:update', 'vault:delete']::text[]
                ) AS scope
            ) AS widened
        )
        WHERE 'vault:write' = ANY(scopes)
          AND NOT (
              'vault:update' = ANY(scopes) AND 'vault:delete' = ANY(scopes)
          );
        """
    )


def downgrade() -> None:
    # Strip the new scopes before narrowing the constraint, or rows carrying
    # them violate it mid-statement.
    #
    # This cannot restore the pre-upgrade grants: the migration widened some
    # credentials, and by the time anything downgrades, others may have been
    # issued with vault:update or vault:delete and no vault:write at all. Those
    # lose the capability entirely, because the old vocabulary has no way to say
    # it. Recorded rather than worked around -- a downgrade here is a schema
    # rollback, not an authorization rollback, and the honest failure is a
    # credential that stops working rather than one that silently keeps a
    # permission the schema no longer knows about.
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
