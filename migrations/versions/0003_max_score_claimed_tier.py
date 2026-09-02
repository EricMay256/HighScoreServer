"""game_modes.max_score ceiling and runs.claimed_tier

Phase 2 of validated runs (see docs/specs.md "per-mode score ceiling"). Two
not-yet-shipped, additive nullable columns bundled into a SINGLE revision
(legitimate because neither has shipped — distinct from folding into 0002's
already-deployed game_modes ALTER):

  * game_modes.max_score  — per-mode ceiling on the canonical/raw score. NULL
                            inherits the global MAX_SCORE; non-NULL caps the
                            mode. BIGINT (not the spec's stale "INTEGER"): a
                            ceiling near MAX_SCORE (~1.8e11) overflows int32,
                            and it must compare against the BIGINT score
                            columns (scores.score, runs.claimed_score).
  * runs.claimed_tier     — the tier the client asserts its log supports.
                            Recorded, never trusted: the validator records the
                            achieved tier independently.

Both are additive (nullable, default NULL, no backfill) and inherit the tables'
existing privileges, so NO new grant is needed. Structural DDL only — grants
live in db/role.sql (see 0002's note and docs/specs.md grant split).

Revision ID: 0003_max_score_claimed_tier
Revises: 0002_runs_cumulative_validation
Create Date: 2026-06-07
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0003_max_score_claimed_tier"
down_revision = "0002_runs_cumulative_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # game_modes.max_score: nullable ceiling, NULL = inherit global MAX_SCORE.
    # CHECK mirrors the table's existing constraint discipline (a negative
    # ceiling is meaningless); the Pydantic write model enforces the same bound.
    op.execute(
        """
        ALTER TABLE game_modes
            ADD COLUMN max_score bigint;

        ALTER TABLE game_modes
            ADD CONSTRAINT game_modes_max_score_check
                CHECK (max_score IS NULL OR max_score >= 0);
        """
    )

    # runs.claimed_tier: recorded-not-trusted client assertion. Same 0..3 domain
    # as required_tier / validation_tier; nullable (omitted when the client does
    # not claim a tier — validation then targets the mode's required_tier).
    op.execute(
        """
        ALTER TABLE runs
            ADD COLUMN claimed_tier smallint;

        ALTER TABLE runs
            ADD CONSTRAINT runs_claimed_tier_check
                CHECK (claimed_tier IS NULL OR claimed_tier BETWEEN 0 AND 3);
        """
    )


def downgrade() -> None:
    # For local/test use only — never run against a database with real data.
    op.execute(
        """
        ALTER TABLE runs
            DROP CONSTRAINT IF EXISTS runs_claimed_tier_check;
        ALTER TABLE runs
            DROP COLUMN IF EXISTS claimed_tier;
        """
    )

    op.execute(
        """
        ALTER TABLE game_modes
            DROP CONSTRAINT IF EXISTS game_modes_max_score_check;
        ALTER TABLE game_modes
            DROP COLUMN IF EXISTS max_score;
        """
    )
