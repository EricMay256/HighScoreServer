"""oauth refresh tokens, and a csrf token on the pending authorization

Two additions the 2026-08-21 amendment to vault ADR 0024 requires.

**Refresh tokens, with rotation and replay detection.** The ADR says OAuth
credentials should expire rather than living forever, and left open how a client
renews one. Without refresh, renewal is the operator redoing the browser flow;
with it, the access credential can be short-lived and the client renews itself.
OAuth 2.1 requires that a public client's refresh token be either
sender-constrained or **rotated with replay detection**, and rotation is the one
available here.

Replay detection is why this table has a `consumed_at` and the other two
transient tables deliberately do not. `vault_oauth_pending_authorizations` and
`vault_oauth_authorization_codes` redeem with `DELETE ... RETURNING`, because for
them a replay is indistinguishable from garbage and nothing useful follows from
telling them apart. A rotated refresh token is different: presenting one is
positive evidence that a token was captured, and the response is to revoke the
whole `family_id` chain rather than merely to fail this request. That requires
remembering the consumed digest, so consumption marks rather than deletes and a
pruning pass removes the rows later.

`family_id` is constant across every rotation descending from one authorization,
which is what makes "revoke the chain" expressible. It is not a foreign key: the
family outlives any particular row in it, which is the point.

`credential_id` references `vault_agent_credentials` because a refresh token
exists to renew exactly one access credential, and a rotation revokes the old
credential as it mints the next. `ON DELETE CASCADE`: a refresh token for a
deleted credential can renew nothing. Note that ordinary revocation sets
`revoked_at` and does not delete, so this cascade fires only when a row is
really removed.

**`csrf_sha256` on the pending authorization.** ADR 0024 requires CSRF
protection on the login POST, and `docs/NEXT-STEPS.md` suggested "a signed hidden
token tied to the nonce". A signed token needs a signing key, which would be a
third secret to configure, rotate and get wrong. A server-side token needs
neither, is single-use for free, and there is already a per-authorization row to
hang it on -- so the token is random, its digest lives here, and the plaintext
goes in the form's hidden field. Hashed for the reason every other secret in this
schema is (ADR 0015).

Nullable rather than NOT NULL. The column is written whenever
`/authorize` creates a row, so it is always present in practice, and the login
route refuses a NULL as it would a mismatch -- but a NOT NULL column added to a
table that could hold rows needs a backfill value, and inventing one for a
security token is worse than handling the absent case explicitly.

The revision id is kept under 32 characters because
`vault_alembic_version.version_num` is `varchar(32)`.

Revision ID: 0014_oauth_refresh_and_csrf
Revises: 0013_oauth_authorization_server
Create Date: 2026-08-21
"""

from alembic import op


revision = "0014_oauth_refresh_and_csrf"
down_revision = "0013_oauth_authorization_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_oauth_pending_authorizations
            ADD COLUMN csrf_sha256 BYTEA;

        ALTER TABLE vault.vault_oauth_pending_authorizations
            ADD CONSTRAINT vault_oauth_pending_csrf_length
            CHECK (csrf_sha256 IS NULL OR octet_length(csrf_sha256) = 32);
        """
    )

    op.execute(
        """
        CREATE TABLE vault.vault_oauth_refresh_tokens (
            token_sha256 BYTEA PRIMARY KEY,
            family_id UUID NOT NULL,
            client_id TEXT NOT NULL
                REFERENCES vault.vault_oauth_clients (client_id)
                ON DELETE CASCADE,
            credential_id TEXT NOT NULL
                REFERENCES vault.vault_agent_credentials (id)
                ON DELETE CASCADE,
            scopes TEXT[] NOT NULL DEFAULT '{}'::text[],
            subject TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            CONSTRAINT vault_oauth_refresh_token_length
                CHECK (octet_length(token_sha256) = 32),
            CONSTRAINT vault_oauth_refresh_expires_after_creation
                CHECK (expires_at > created_at),
            CONSTRAINT vault_oauth_refresh_scopes_known
                CHECK (scopes <@ ARRAY['vault:read', 'vault:write',
                    'vault:update', 'vault:delete', 'vault:review',
                    'vault:compile', 'vault:export']::text[])
        );
        """
    )
    # Revoking a family is the replay response, so it has to be a lookup rather
    # than a scan. Not unique: every rotation in one chain shares the value,
    # which is the whole idea.
    op.execute(
        """
        CREATE INDEX vault_oauth_refresh_family_idx
            ON vault.vault_oauth_refresh_tokens (family_id);
        """
    )
    op.execute(
        """
        CREATE INDEX vault_oauth_refresh_expires_at_idx
            ON vault.vault_oauth_refresh_tokens (expires_at);
        """
    )


def downgrade() -> None:
    """Drops cleanly, for the reason 0013's downgrade gives.

    Everything here is transient authorization state a client can re-establish
    by authorizing again. The access credentials themselves are
    `vault_agent_credentials` rows and are untouched -- losing their refresh
    tokens strands them until they expire, which is a re-authorization rather
    than lost data.
    """

    op.execute(
        """
        DROP TABLE IF EXISTS vault.vault_oauth_refresh_tokens;

        ALTER TABLE vault.vault_oauth_pending_authorizations
            DROP CONSTRAINT IF EXISTS vault_oauth_pending_csrf_length;
        ALTER TABLE vault.vault_oauth_pending_authorizations
            DROP COLUMN IF EXISTS csrf_sha256;
        """
    )
