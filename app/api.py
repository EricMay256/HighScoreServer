import json
import logging
from datetime import timezone
from fastapi import APIRouter, Depends, HTTPException, status
from app.models import ScoreSubmission, ScoreResponse, GameModeConfig, GameModeCreate
from app.db import get_conn, release_conn
from app.cache import get_cache
from app.dependencies import require_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "leaderboard:"
CACHE_TTL = 120  # seconds

@router.get("/game_modes", response_model=list[GameModeConfig])
def list_game_modes() -> list[GameModeConfig]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, sort_order, label FROM game_modes ORDER BY name")
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return [GameModeConfig(name=r[0], sort_order=r[1], label=r[2]) for r in rows]


@router.post(
    "/game_modes",
    response_model=GameModeConfig,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def create_game_mode(config: GameModeCreate) -> GameModeConfig:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO game_modes (name, sort_order, label)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    sort_order = EXCLUDED.sort_order,
                    label      = EXCLUDED.label
                RETURNING name, sort_order, label
                """,
                (config.name, config.sort_order, config.label),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return GameModeConfig(name=row[0], sort_order=row[1], label=row[2])

@router.get("/scores/latest", response_model=list[ScoreResponse])
def all_scores() -> list[ScoreResponse]:
    # Attempt cache read — fall through to DB if Redis is unavailable
    try:
        cache = get_cache()
        cached = cache.get(f"{CACHE_KEY_PREFIX}")
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, player, score, game_mode, submitted_at
                FROM scores
                ORDER BY submitted_at DESC
                LIMIT 100
                """,
            )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        release_conn(conn)

    return [
        ScoreResponse(
            id=row[0],
            player=row[1],
            score=row[2],
            game_mode=row[3],
            submitted_at=row[4].replace(tzinfo=timezone.utc).isoformat(),
        )
        for row in rows
    ]

@router.get("/scores", response_model=list[ScoreResponse])
def get_scores(game_mode: str) -> list[ScoreResponse]:
    try:
        cache = get_cache()
        cached = cache.get(f"{CACHE_KEY_PREFIX}{game_mode}")
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sort_order FROM game_modes WHERE name = %s",
                (game_mode,),
            )
            mode_row = cur.fetchone()
            if mode_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Unknown game mode: {game_mode}",
                )

            order = "ASC" if mode_row[0] == "ASC" else "DESC"

            cur.execute(
                f"""
                SELECT id, player, score, game_mode, submitted_at
                FROM scores
                WHERE game_mode = %s
                ORDER BY score {order}
                LIMIT 100
                """,
                (game_mode,),
            )
            rows = cur.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    results = [
        ScoreResponse(
            id=row[0], player=row[1], score=row[2],
            game_mode=row[3],
            submitted_at=row[4].replace(tzinfo=timezone.utc).isoformat(),
        )
        for row in rows
    ]

    try:
        get_cache().setex(
            f"{CACHE_KEY_PREFIX}{game_mode}",
            CACHE_TTL,
            json.dumps([r.model_dump() for r in results]),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return results

@router.post(
    "/scores",
    response_model=ScoreResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def submit_score(submission: ScoreSubmission) -> ScoreResponse:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scores (player, score, game_mode)
                VALUES (%s, %s, %s)
                ON CONFLICT (player, game_mode)
                DO UPDATE SET
                    score = EXCLUDED.score,
                    submitted_at = NOW()
                WHERE scores.score < EXCLUDED.score
                RETURNING id, player, score, game_mode, submitted_at
                """,
                (submission.player, submission.score, submission.game_mode),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        release_conn(conn)

    if row is None:
        return get_scores(submission.game_mode)[0]

    result = ScoreResponse(
        id=row[0],
        player=row[1],
        score=row[2],
        game_mode=row[3],
        submitted_at=row[4].replace(tzinfo=timezone.utc).isoformat(),
    )

    # Attempt cache invalidation — non-fatal if Redis is unavailable
    try:
        get_cache().delete(f"{CACHE_KEY_PREFIX}{submission.game_mode}")
    except Exception as e:
        logger.warning("Redis cache invalidation failed, continuing: %s", e)

    return result