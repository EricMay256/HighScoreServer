"""first-class auth identities

Separates the durable leaderboard account (`users`) from the ways a player can
prove control of that account. Existing email/password accounts are represented
as provider `ubear`; external providers such as Steam or Epic can be attached as
additional rows without adding nullable columns to `users`.

Structural DDL only. Least-privilege grants live in db/role.sql so Heroku's
owner-only production database never runs role-specific GRANT statements.

Revision ID: 0004_auth_identities
Revises: 0003_max_score_claimed_tier
Create Date: 2026-07-10
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_auth_identities"
down_revision = "0003_max_score_claimed_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_identities (
            id serial NOT NULL,
            user_id integer NOT NULL,
            provider character varying(32) NOT NULL,
            provider_user_id text NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT auth_identities_pkey PRIMARY KEY (id),
            CONSTRAINT auth_identities_provider_user_id_key
                UNIQUE (provider, provider_user_id),
            CONSTRAINT auth_identities_provider_check
                CHECK (provider ~ '^[a-z][a-z0-9_]{0,31}$'),
            CONSTRAINT auth_identities_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_auth_identities_user_id ON auth_identities
            USING btree (user_id);
        """
    )

    # Backfill native email/password identities for already-claimed accounts.
    # Guests have no durable authenticator yet, so they intentionally get no row.
    op.execute(
        """
        INSERT INTO auth_identities (user_id, provider, provider_user_id)
        SELECT id, 'ubear', email
        FROM users
        WHERE email IS NOT NULL
        ON CONFLICT (provider, provider_user_id) DO NOTHING;
        """
    )


def downgrade() -> None:
    # For local/test use only - never run against a database with real data.
    op.execute("DROP TABLE IF EXISTS auth_identities;")
