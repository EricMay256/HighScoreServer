"""persistent operator-granted OAuth entitlements

An OAuth access credential rotates every hour, so widening that credential row
is necessarily temporary. This revision makes the authorization grant -- the
refresh family -- the durable authority object. Consented baseline scopes and
operator-only entitlements are stored separately and every access credential is
projected from their union.

Existing families are backfilled with only their OAuth-baseline scopes and no
operator entitlements. That is deliberately fail-closed: no historical
credential-level widening silently becomes permanent during deployment.

Revision ID: 0017_oauth_entitlements
Revises: 0016_amendment_proposals
Create Date: 2026-08-25
"""

from alembic import op


revision = "0017_oauth_entitlements"
down_revision = "0016_amendment_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vault.vault_oauth_grants (
            family_id uuid PRIMARY KEY,
            client_id text NOT NULL,
            authorized_scopes text[] NOT NULL DEFAULT '{}'::text[],
            entitled_scopes text[] NOT NULL DEFAULT '{}'::text[],
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT vault_oauth_grants_client_id_fkey
                FOREIGN KEY (client_id)
                REFERENCES vault.vault_oauth_clients(client_id)
                ON DELETE CASCADE,
            CONSTRAINT vault_oauth_grants_authorized_scopes_baseline
                CHECK (authorized_scopes <@ ARRAY[
                    'vault:read', 'vault:write', 'vault:propose'
                ]::text[]),
            CONSTRAINT vault_oauth_grants_entitled_scopes_privileged
                CHECK (entitled_scopes <@ ARRAY[
                    'vault:update', 'vault:delete', 'vault:review',
                    'vault:compile', 'vault:export'
                ]::text[]),
            CONSTRAINT vault_oauth_grants_scope_sets_disjoint
                CHECK (NOT (authorized_scopes && entitled_scopes))
        );

        WITH latest_family_token AS (
            SELECT DISTINCT ON (family_id)
                family_id,
                client_id,
                scopes
            FROM vault.vault_oauth_refresh_tokens
            ORDER BY family_id, created_at DESC
        )
        INSERT INTO vault.vault_oauth_grants (
            family_id,
            client_id,
            authorized_scopes,
            entitled_scopes
        )
        SELECT
            family_id,
            client_id,
            ARRAY(
                SELECT DISTINCT scope
                FROM unnest(scopes) AS scope
                WHERE scope = ANY (ARRAY[
                    'vault:read', 'vault:write', 'vault:propose'
                ]::text[])
                ORDER BY scope
            ),
            '{}'::text[]
        FROM latest_family_token;

        ALTER TABLE vault.vault_oauth_refresh_tokens
            ADD CONSTRAINT vault_oauth_refresh_tokens_family_id_fkey
            FOREIGN KEY (family_id)
            REFERENCES vault.vault_oauth_grants(family_id)
            ON DELETE CASCADE;

        ALTER TABLE vault.vault_oauth_authorization_codes
            DROP CONSTRAINT vault_oauth_codes_scopes_known,
            ADD CONSTRAINT vault_oauth_codes_scopes_known
                CHECK (scopes <@ ARRAY[
                    'vault:read', 'vault:write', 'vault:propose'
                ]::text[]);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_oauth_refresh_tokens
            DROP CONSTRAINT vault_oauth_refresh_tokens_family_id_fkey;
        ALTER TABLE vault.vault_oauth_authorization_codes
            DROP CONSTRAINT vault_oauth_codes_scopes_known,
            ADD CONSTRAINT vault_oauth_codes_scopes_known
                CHECK (scopes <@ ARRAY[
                    'vault:read', 'vault:write', 'vault:propose',
                    'vault:update', 'vault:delete', 'vault:review',
                    'vault:compile', 'vault:export'
                ]::text[]);
        DROP TABLE vault.vault_oauth_grants;
        """
    )
