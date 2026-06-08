from pydantic import BaseModel, Field
from typing import Any, Literal

# Shared bounds
# Arbitrary upper limit to prevent abuse; the C# client's score field (long)
# must honor this cap. Used by both raw score submissions and a run's
# recorded claimed_score.
MAX_SCORE = 180_000_000_081
# Transport bound on a run's action log — element count only. This is a
# coarse abuse guard, not a semantic check; the real per-scenario bounds live
# with the validator (Phase 3/4). Decompressed-byte-size is enforced at the
# /runs endpoint where the raw payload is available, not in this model.
MAX_RUN_ACTIONS = 50_000

#Game mode models
class GameModeConfig(BaseModel):
    name:         str
    sort_order:   str  # 'ASC' | 'DESC'
    label:        str | None = None
    requires_claimed_account:   bool = False
    # Surfaced read-only so clients *can* read them; no client-side enforcement
    # this phase. required_tier 0 = raw via /scores, >=1 = run required via /runs.
    required_tier:    int = 0
    scoring_strategy: str = "best"  # 'best' | 'cumulative'
    game_key:         str | None = None  # forward-provisioning; nothing reads it yet
    # Per-mode score ceiling. None inherits the global MAX_SCORE; non-None caps
    # the canonical/raw score for this mode (enforced at tier 0 on /scores and
    # by the validator on /runs).
    max_score:        int | None = None

class GameModeCreate(BaseModel):
    name:         str = Field(..., min_length=1, max_length=32)
    sort_order:   str = Field("DESC", pattern="^(ASC|DESC)$")
    label:        str | None = Field(None, max_length=128)
    requires_claimed_account:   bool = Field(False)
    # Operator-settable. Defaults keep existing create calls behaving identically.
    required_tier:    int = Field(0, ge=0, le=3)
    scoring_strategy: str = Field("best", pattern="^(best|cumulative)$")
    game_key:         str | None = Field(None, max_length=64)
    # Operator-settable per-mode ceiling. None inherits the global MAX_SCORE; a
    # value above the global makes no sense, so it is capped there.
    max_score:        int | None = Field(None, ge=0, le=MAX_SCORE)

#Score models
class ScoreSubmission(BaseModel):
    score:      int = Field(..., ge=0, le=MAX_SCORE)
    game_mode:  str = Field(..., min_length=1, max_length=32)
    # Required only when the target mode is cumulative (dedup key). Validated
    # in the handler against the looked-up mode — the requirement is
    # data-dependent and can't be expressed in the model alone.
    idempotency_key: str | None = Field(None, min_length=8, max_length=128)

class ScoreResponse(BaseModel):
    id:           int
    player:       str
    score:        int
    game_mode:    str
    period:       str | None = None
    submitted_at: str  # ISO 8601 string — easier to serialize across the boundary
    rank:         int | None = None  # Optional, only included in certain responses
    percentile:   float | None = None #0.0 to 100.0, two decimal places
    # Raw and cumulative submissions return validated=False, validation_tier=0.
    # A score produced by a validated run sets these (Phase 3+, via scores.run_id).
    validated:        bool = False
    validation_tier:  int = 0

#Run models
class RunSubmission(BaseModel):
    """A submitted run that the server validates to produce a canonical score.

    The action log is opaque at the API boundary: `actions` is validated for
    transport bounds only (it is a list, capped element count), never internal
    semantics. The stored blob is gzipped regardless of element shape, and
    `scenario_version` is the key a future validator uses to pick the parser.
    """
    game_mode:        str = Field(..., min_length=1, max_length=32)
    scenario_version: int = Field(..., ge=1)
    seed:             int
    actions:          list[Any] = Field(..., max_length=MAX_RUN_ACTIONS)
    claimed_score:    int | None = Field(None, ge=0, le=MAX_SCORE)
    client_run_id:    str = Field(..., min_length=8, max_length=128)
    # The tier the client asserts its log supports. Recorded, never trusted:
    # persisted on the run, but the validator records the achieved tier itself.
    # When omitted, validation targets the mode's required_tier.
    claimed_tier:     int | None = Field(None, ge=0, le=3)

class LeaderboardResponse(BaseModel):
    scores:      list[ScoreResponse]
    total_count: int

#Period based leaderboard queries
# Maintain against app/periods.py:PERIODS
Period = Literal["alltime", "daily", "weekly"]
class LeaderboardQuery(BaseModel):
    game_mode: str = Field(..., min_length=1, max_length=32)
    period: Period = "alltime"
