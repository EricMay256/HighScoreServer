"""oauth authorization server state

The persistence half of vault ADR 0024. Three tables, none of which holds a
token: an access token *is* a `vault_agent_credentials` row, which is the whole
of that decision and the reason there is no fourth table here.

**Postgres, not process memory, and this is the constraint the spike found.**
Client registration arrives server-to-server from the vendor's backend while
`/authorize` is a browser navigation from the operator's machine. The two halves
reliably land on different Gunicorn workers, so an in-memory dict fails
deterministically -- and only in production, where there is more than one
worker. `app/vault/oauth_spike.py` used a dict, failed exactly there, and was
moved to a file on the dyno filesystem to get an answer at all.

**`vault_oauth_clients.client_info` is JSONB rather than a column per field.**
The SDK owns `OAuthClientInformationFull`, and RFC 7591 lets a registration
carry metadata neither this schema nor that model anticipated. Projecting it
into columns would silently drop whatever did not fit and would need a migration
every time the SDK grew a field. Same reasoning `vault_documents.frontmatter`
already carries. `client_id` is lifted out because it is the lookup key, and
`expires_at` because pruning has to see it without parsing every blob.

**Secrets are stored as SHA-256, never in the clear.** ADR 0015's rule: an
authorization code is machine-generated with full entropy, so a plain digest is
correct and a password KDF would be the wrong tool. It applies here and does
*not* apply to the operator password, which is bcrypt and does not live in this
database at all.

**Single-use is `DELETE ... RETURNING`, which is why neither transient table has
a `consumed_at`.** The host's refresh-token rotation already uses that idiom for
the same reason: a check-then-mark is two statements a concurrent redemption can
interleave, while a conditional delete either returns the row or does not.
Expired rows accumulate for a pruning script, in the shape
`prune_idempotency_keys.py` established.

`vault_oauth_pending_authorizations.client_id` carries a real foreign key,
unlike the correlation identifiers ADR 0002 keeps FK-free. The distinction is
what the row is: an audit event is a durable record that must never fail to
insert, while a pending authorization is transient state that is meaningless
without its client. `ON DELETE CASCADE` for the same reason -- an authorization
in flight for a client that has just been pruned cannot complete.

The revision id is kept under 32 characters because
`vault_alembic_version.version_num` is `varchar(32)`.

Revision ID: 0013_oauth_authorization_server
Revises: 0012_document_promotion_status
Create Date: 2026-08-21
"""

from alembic import op


revision = "0013_oauth_authorization_server"
down_revision = "0012_document_promotion_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vault.vault_oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_info JSONB NOT NULL,
            registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            CONSTRAINT vault_oauth_clients_client_id_nonempty
                CHECK (btrim(client_id) <> ''),
            CONSTRAINT vault_oauth_clients_info_is_object
                CHECK (jsonb_typeof(client_info) = 'object')
        );
        """
    )
    # Pruning walks by expiry. Partial, because a client with no expiry is
    # never a candidate and indexing those rows costs writes to answer nothing.
    op.execute(
        """
        CREATE INDEX vault_oauth_clients_expires_at_idx
            ON vault.vault_oauth_clients (expires_at)
            WHERE expires_at IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE TABLE vault.vault_oauth_pending_authorizations (
            nonce_sha256 BYTEA PRIMARY KEY,
            client_id TEXT NOT NULL
                REFERENCES vault.vault_oauth_clients (client_id)
                ON DELETE CASCADE,
            params JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT vault_oauth_pending_nonce_length
                CHECK (octet_length(nonce_sha256) = 32),
            CONSTRAINT vault_oauth_pending_params_is_object
                CHECK (jsonb_typeof(params) = 'object'),
            CONSTRAINT vault_oauth_pending_expires_after_creation
                CHECK (expires_at > created_at)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX vault_oauth_pending_expires_at_idx
            ON vault.vault_oauth_pending_authorizations (expires_at);
        """
    )

    # `scopes` is a TEXT[] column here rather than a key inside a JSONB blob,
    # unlike client_info: this is a set the vault itself decides and hands to
    # `vault_agent_credentials.scopes`, so the two should have the same shape
    # and be readable by the same query. The CHECK deliberately mirrors
    # vault_agent_credentials_scopes_known rather than the narrower OAuth
    # baseline -- ADR 0024 makes the baseline what a client may *request*,
    # enforced in application code, while an operator may widen a specific
    # credential afterwards. A stricter constraint here would forbid a code
    # minted for a widened client, which is the case the ADR calls expected.
    op.execute(
        """
        CREATE TABLE vault.vault_oauth_authorization_codes (
            code_sha256 BYTEA PRIMARY KEY,
            client_id TEXT NOT NULL
                REFERENCES vault.vault_oauth_clients (client_id)
                ON DELETE CASCADE,
            scopes TEXT[] NOT NULL DEFAULT '{}'::text[],
            code_challenge TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            redirect_uri_provided_explicitly BOOLEAN NOT NULL,
            resource TEXT,
            subject TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT vault_oauth_codes_code_length
                CHECK (octet_length(code_sha256) = 32),
            CONSTRAINT vault_oauth_codes_challenge_nonempty
                CHECK (btrim(code_challenge) <> ''),
            CONSTRAINT vault_oauth_codes_redirect_uri_nonempty
                CHECK (btrim(redirect_uri) <> ''),
            CONSTRAINT vault_oauth_codes_expires_after_creation
                CHECK (expires_at > created_at),
            CONSTRAINT vault_oauth_codes_scopes_known
                CHECK (scopes <@ ARRAY['vault:read', 'vault:write',
                    'vault:update', 'vault:delete', 'vault:review',
                    'vault:compile', 'vault:export']::text[])
        );
        """
    )
    op.execute(
        """
        CREATE INDEX vault_oauth_codes_expires_at_idx
            ON vault.vault_oauth_authorization_codes (expires_at);
        """
    )


def downgrade() -> None:
    """Drops cleanly, unlike 0011 and 0012, and for a stated reason.

    Every row in all three tables is transient authorization state: a
    registration a client can repeat, a nonce that expires in minutes, a code
    that expires in seconds. None of it is a judgement somebody made, so there
    is nothing here a rollback would destroy that cannot simply happen again.

    The credentials OAuth mints are not dropped, because they are not here --
    they are ordinary `vault_agent_credentials` rows and survive this untouched,
    which is ADR 0024's point.
    """

    op.execute(
        """
        DROP TABLE IF EXISTS vault.vault_oauth_authorization_codes;
        DROP TABLE IF EXISTS vault.vault_oauth_pending_authorizations;
        DROP TABLE IF EXISTS vault.vault_oauth_clients;
        """
    )
