"""baseline: existing HSS schema (public objects only)

Represents the live application schema at the point of Alembic adoption.

On the already-deployed database these objects ALREADY EXIST, so this
revision is never run with `upgrade` there. Instead the database is marked
as already-at-this-revision:

    alembic stamp 0001_baseline

A fresh/empty database (local dev, CI, a test DB) builds the whole schema:

    alembic upgrade head

Heroku platform objects — the _heroku schema and its functions, the
pg_stat_statements extension, and the extension-management event triggers —
are deliberately excluded. They are provisioned by the platform, are not part
of the application schema, and must not be recreated locally.

Constraint and index names are spelled out explicitly to match the names in
the production database, so a schema-only dump of a fresh `upgrade head`
build compares byte-identical (within the public schema) to production.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-30
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tables created in dependency order so inline foreign keys resolve:
    # game_modes and users have no dependencies; refresh_tokens and scores
    # reference users (and scores references game_modes).
    op.execute(
        """
        CREATE TABLE game_modes (
            name character varying(32) NOT NULL,
            sort_order character varying(4) DEFAULT 'DESC' NOT NULL,
            label text,
            requires_claimed_account boolean DEFAULT false NOT NULL,
            CONSTRAINT game_modes_pkey PRIMARY KEY (name),
            CONSTRAINT game_modes_sort_order_check
                CHECK (sort_order IN ('ASC', 'DESC'))
        );

        CREATE TABLE users (
            id serial NOT NULL,
            username character varying(64) NOT NULL,
            email character varying(256),
            password_hash text,
            is_guest boolean DEFAULT false NOT NULL,
            is_verified boolean DEFAULT false NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT users_pkey PRIMARY KEY (id),
            CONSTRAINT users_username_key UNIQUE (username)
        );

        CREATE TABLE refresh_tokens (
            id serial NOT NULL,
            user_id integer NOT NULL,
            token_hash text NOT NULL,
            expires_at timestamp with time zone NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT refresh_tokens_pkey PRIMARY KEY (id),
            CONSTRAINT refresh_tokens_token_hash_key UNIQUE (token_hash),
            CONSTRAINT refresh_tokens_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE scores (
            id serial NOT NULL,
            user_id integer NOT NULL,
            game_mode character varying(32) NOT NULL,
            score bigint NOT NULL,
            period character varying(16) NOT NULL,
            period_start timestamp with time zone NOT NULL,
            submitted_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT scores_pkey PRIMARY KEY (id),
            CONSTRAINT scores_user_id_game_mode_period_period_start_key
                UNIQUE (user_id, game_mode, period, period_start),
            CONSTRAINT scores_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE RESTRICT,
            CONSTRAINT scores_game_mode_fkey FOREIGN KEY (game_mode)
                REFERENCES game_modes(name)
        );
        """
    )

    op.execute(
        """
        CREATE INDEX idx_scores_lookup_asc ON scores
            USING btree (game_mode, period, period_start, score, submitted_at, id, user_id);

        CREATE INDEX idx_scores_lookup_desc ON scores
            USING btree (game_mode, period, period_start, score DESC, submitted_at, id, user_id);

        CREATE UNIQUE INDEX idx_users_email ON users
            USING btree (email) WHERE (email IS NOT NULL);
        """
    )


def downgrade() -> None:
    # Baseline teardown for local/test use. CASCADE clears the dependent
    # sequences, indexes, and foreign keys. Never run against production.
    op.execute("DROP TABLE IF EXISTS scores, refresh_tokens, users, game_modes CASCADE;")