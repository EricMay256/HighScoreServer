import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.db import get_conn, release_conn

router = APIRouter(tags=["views"])
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")

@router.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_view(request: Request, game_mode: str = "classic") -> HTMLResponse:
    """
    Renders the leaderboard page for a given game mode.
    Falls back to an empty list on DB error rather than raising — this is a
    view, not an API endpoint, so a full 500 page would be worse UX.
    """
    conn = get_conn()
    scores = []
    game_modes = []
    score_label = "Score"
    sort_order = "DESC"
    error: str | None = None

    try:
        with conn.cursor() as cur:
            # Pull tab list and mode config from game_modes table
            cur.execute("SELECT name, sort_order, label FROM game_modes ORDER BY name")
            rows = cur.fetchall()
            game_modes = [r[0] for r in rows]
            mode_map = {r[0]: {"sort_order": r[1], "label": r[2]} for r in rows}

            config = mode_map.get(game_mode)
            if config:
                sort_order = config["sort_order"]
                score_label = config["label"] or "Score"

            # sort_order comes from the DB, never from user input — safe to interpolate
            cur.execute(
                f"""
                SELECT player, score, submitted_at
                FROM leaderboard_snapshots
                WHERE game_mode = %s
                AND period = 'alltime' 
                ORDER BY score {sort_order}
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
            "game_modes": game_modes,
            "scores": scores,
            "score_label": score_label,
            "sort_order": sort_order,
            "error": error,
        },
    )
