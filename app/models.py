from pydantic import BaseModel, Field


class ScoreSubmission(BaseModel):
    player: str = Field(..., min_length=1, max_length=64)
    score: int = Field(..., ge=0)
    game_mode: str = Field(..., min_length=1, max_length=32)


class ScoreResponse(BaseModel):
    id: int
    player: str
    score: int
    game_mode: str
    submitted_at: str  # ISO 8601 string — easier to serialize across the boundary