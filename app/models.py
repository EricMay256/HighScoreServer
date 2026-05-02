from pydantic import BaseModel, Field
from typing import Literal

#Game mode models
class GameModeConfig(BaseModel):
    name:         str
    sort_order:   str  # 'ASC' | 'DESC'
    label:        str | None = None
    requires_claimed_account:   bool = False

class GameModeCreate(BaseModel):
    name:         str = Field(..., min_length=1, max_length=32)
    sort_order:   str = Field("DESC", pattern="^(ASC|DESC)$")
    label:        str | None = Field(None, max_length=128)
    requires_claimed_account:   bool = Field(False)

#Score models
class ScoreSubmission(BaseModel):
    score:      int = Field(..., ge=0, le=18_000_000_420)  # Arbitrary upper limit to prevent abuse
    game_mode:  str = Field(..., min_length=1, max_length=32)

class ScoreResponse(BaseModel):
    id:           int
    player:       str
    score:        int
    game_mode:    str
    period:       str | None = None
    submitted_at: str  # ISO 8601 string — easier to serialize across the boundary
    rank:         int | None = None  # Optional, only included in certain responses
    percentile:   float | None = None #0.0 to 100.0, two decimal places

class LeaderboardResponse(BaseModel):
    scores:      list[ScoreResponse]
    total_count: int

#Period based leaderboard queries
# Maintain against app/periods.py:PERIODS
Period = Literal["alltime", "daily", "weekly"]
class LeaderboardQuery(BaseModel):
    game_mode: str = Field(..., min_length=1, max_length=32)
    period: Period = "alltime"

