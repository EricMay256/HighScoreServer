from pydantic import BaseModel, Field
from typing import Literal

#Game mode models
class GameModeConfig(BaseModel):
    name:           str
    sort_order:     str  # 'ASC' | 'DESC'
    label:          str | None = None
    requires_auth:  bool = False

class GameModeCreate(BaseModel):
    name:           str = Field(..., min_length=1, max_length=32)
    sort_order:     str = Field("DESC", pattern="^(ASC|DESC)$")
    label:          str | None = Field(None, max_length=128)
    requires_auth:  bool = Field(False)

#Score models
class ScoreSubmission(BaseModel):
    score:      int = Field(..., ge=0, le=18_000_000_420)  # Arbitrary upper limit to prevent abuse
    game_mode:  str = Field(..., min_length=1, max_length=32)

class ScoreResponse(BaseModel):
    id:           int
    player:       str
    score:        int
    game_mode:    str
    period:       str
    submitted_at: str  # ISO 8601 string — easier to serialize across the boundary

#Period based leaderboard queries
# Maintain against app/periods.py:PERIODS
Period = Literal["alltime", "daily", "weekly"]
class LeaderboardQuery(BaseModel):
    game_mode: str = Field(..., min_length=1, max_length=32)
    period: Period = "alltime"

