"""runs, cumulative scoring, and validated-run schema

Phase 1 of the validated-runs / cumulative-scoring feature (see docs/specs.md).
All changes are additive with safe defaults so existing rows and modes need
no backfill:

  * new table `runs`                 — a submitted run that *produces* a score
  * new table `submission_idempotency`— dedup for cumulative raw (tier-0) modes
  * alter  `game_modes`              — required_tier, scoring_strategy, game_key
  * alter  `scores`                  — run_id link back to the producing run

Structural DDL ONLY. Grants are deliberately NOT here: prod runs as a single
owner role that has no `leaderboard_app`, so a `GRANT ... TO leaderboard_app`
in a revision would error on prod and (under release-phase migrations) abort
the deploy. Grants live in db/role.sql, applied per-environment. See docs/specs.md
"Grants and the migration/grant split".

Revision ID: 0002_runs_cumulative_validation
Revises: 0001_baseline
Create Date: 2026-05-31
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_runs_cumulative_validation"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `runs` first: scores.run_id references it, so it must exist before the
    # scores alter below. References users and game_modes, which already exist.
    #
    #   user_id ON DELETE RESTRICT  — a run is leaderboard history; never orphan
    #                                 it (mirrors scores.user_id).
    #   claimed_score               — recorded, never trusted.
    #   canonical_score             — server-computed; null until validated.
    #   validation_tier             — tier actually achieved; null until validated.
    #   status                      — pending | validated | rejected.
    #   client_run_id               — idempotency + anti-replay, unique per
    #                                 (user_id, game_mode).
    #   actions                     — gzipped JSON action log (single blob, not
    #                                 a normalized per-action table).
    op.execute(
        """
        CREATE TABLE runs (
            id serial NOT NULL,
            user_id integer NOT NULL,
            game_mode character varying(32) NOT NULL,
            scenario_version integer NOT NULL,
            seed bigint NOT NULL,
            claimed_score bigint,
            canonical_score bigint,
            validation_tier smallint,
            status character varying(16) DEFAULT 'pending' NOT NULL,
            client_run_id text NOT NULL,
            actions bytea NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT runs_pkey PRIMARY KEY (id),
            CONSTRAINT runs_status_check
                CHECK (status IN ('pending', 'validated', 'rejected')),
            CONSTRAINT runs_validation_tier_check
                CHECK (validation_tier IS NULL
                       OR validation_tier BETWEEN 0 AND 3),
            CONSTRAINT runs_user_id_game_mode_client_run_id_key
                UNIQUE (user_id, game_mode, client_run_id),
            CONSTRAINT runs_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE RESTRICT,
            CONSTRAINT runs_game_mode_fkey FOREIGN KEY (game_mode)
                REFERENCES game_modes(name)
        );

        CREATE INDEX idx_runs_game_mode_status_created ON runs
            USING btree (game_mode, status, created_at);
        """
    )

    # `submission_idempotency`: dedup for cumulative submissions on raw (tier-0)
    # modes, where there is no `runs` row to carry client_run_id. The composite
    # (user_id, game_mode, key) IS the primary key — it doubles as the dedup
    # uniqueness, so no surrogate id/sequence is needed. Write path is
    # INSERT ... ON CONFLICT DO NOTHING; a conflict means duplicate.
    #
    # user_id ON DELETE CASCADE (not RESTRICT): these are ephemeral dedup
    # markers with no archival value, so they must not pin a user against
    # deletion (mirrors refresh_tokens, not scores).
    op.execute(
        """
        CREATE TABLE submission_idempotency (
            user_id integer NOT NULL,
            game_mode character varying(32) NOT NULL,
            key text NOT NULL,
            first_seen timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT submission_idempotency_pkey
                PRIMARY KEY (user_id, game_mode, key),
            CONSTRAINT submission_idempotency_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT submission_idempotency_game_mode_fkey FOREIGN KEY (game_mode)
                REFERENCES game_modes(name)
        );
        """
    )

    # game_modes: the alter that forced Alembic adoption.
    #   required_tier     0 = raw via /scores; >=1 = run required via /runs.
    #   scoring_strategy  best (personal-best upsert) | cumulative (sum).
    #   game_key          forward-provisioning only — nothing reads it this
    #                     phase; tags a mode with its owning game for future
    #                     scoping of aggregate endpoints. Nullable so existing
    #                     modes need no backfill.
    op.execute(
        """
        ALTER TABLE game_modes
            ADD COLUMN required_tier smallint DEFAULT 0 NOT NULL,
            ADD COLUMN scoring_strategy character varying DEFAULT 'best' NOT NULL,
            ADD COLUMN game_key text;

        ALTER TABLE game_modes
            ADD CONSTRAINT game_modes_required_tier_check
                CHECK (required_tier BETWEEN 0 AND 3),
            ADD CONSTRAINT game_modes_scoring_strategy_check
                CHECK (scoring_strategy IN ('best', 'cumulative'));

        CREATE INDEX idx_game_modes_game_key ON game_modes
            USING btree (game_key);
        """
    )

    # scores: link a leaderboard row to the run that produced it (run_id; null
    # for raw submissions), and denormalize that run's validation tier onto the
    # row so the hot read paths (GET /scores, /latest) need no join to runs.
    # validated is derived as validation_tier > 0. Safe to denormalize: a
    # validated run's tier is immutable, so the copy never drifts. Existing
    # rows default to 0 (raw); there are no runs yet at this migration, so no
    # backfill is required.
    op.execute(
        """
        ALTER TABLE scores
            ADD COLUMN run_id integer,
            ADD COLUMN validation_tier smallint DEFAULT 0 NOT NULL;

        ALTER TABLE scores
            ADD CONSTRAINT scores_run_id_fkey FOREIGN KEY (run_id)
                REFERENCES runs(id),
            ADD CONSTRAINT scores_validation_tier_check
                CHECK (validation_tier BETWEEN 0 AND 3);
        """
    )


def downgrade() -> None:
    # Reverse order: drop scores.run_id (it FKs runs) before dropping runs.
    # For local/test use only — never run against a database with real data.
    op.execute(
        """
        ALTER TABLE scores
            DROP CONSTRAINT IF EXISTS scores_run_id_fkey;
        ALTER TABLE scores
            DROP CONSTRAINT IF EXISTS scores_validation_tier_check;
        ALTER TABLE scores
            DROP COLUMN IF EXISTS run_id,
            DROP COLUMN IF EXISTS validation_tier;
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS idx_game_modes_game_key;
        ALTER TABLE game_modes
            DROP CONSTRAINT IF EXISTS game_modes_scoring_strategy_check;
        ALTER TABLE game_modes
            DROP CONSTRAINT IF EXISTS game_modes_required_tier_check;
        ALTER TABLE game_modes
            DROP COLUMN IF EXISTS game_key,
            DROP COLUMN IF EXISTS scoring_strategy,
            DROP COLUMN IF EXISTS required_tier;
        """
    )

    op.execute("DROP TABLE IF EXISTS submission_idempotency;")
    op.execute("DROP TABLE IF EXISTS runs CASCADE;")
