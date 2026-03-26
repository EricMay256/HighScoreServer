import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.db import get_conn, release_conn

router = APIRouter()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")

VALID_GAME_MODES = ["classic", "blitz", "survival"]  # extend as needed


@router.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_view(request: Request, game_mode: str = "classic") -> HTMLResponse:
    """
    Renders the leaderboard page for a given game mode.
    Falls back to an empty list on DB error rather than raising — this is a
    view, not an API endpoint, so a full 500 page would be worse UX.
    """
    conn = get_conn()
    scores = []
    error: str | None = None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT player, score, submitted_at
                FROM scores
                WHERE game_mode = %s
                ORDER BY score DESC
                LIMIT 100
                """,
                (game_mode,),
            )
            rows = cur.fetchall()
            scores = [
                {
                    "rank": i + 1,
                    "player": row[0],
                    "score": row[1],
                    "submitted_at": row[2].strftime("%Y-%m-%d"),
                }
                for i, row in enumerate(rows)
            ]
    except Exception as e:
        logger.error("DB error in leaderboard_view: %s", e)
        error = "Could not load scores. Please try again later."
    finally:
        release_conn(conn)

    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "game_mode": game_mode,
            "game_modes": VALID_GAME_MODES,
            "scores": scores,
            "error": error,
        },
    )