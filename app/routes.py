import json
import logging
from datetime import timezone
from fastapi import APIRouter, Depends, HTTPException, status
from app.models import ScoreSubmission, ScoreResponse
from app.db import get_conn, release_conn
from app.cache import get_cache
from app.dependencies import require_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "leaderboard:"
CACHE_TTL = 120  # seconds


@router.get("/scores", response_model=list[ScoreResponse])
def get_scores(game_mode: str) -> list[ScoreResponse]:
    # Attempt cache read — fall through to DB if Redis is unavailable
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
                """
                SELECT id, player, score, game_mode, submitted_at
                FROM scores
                WHERE game_mode = %s
                ORDER BY score DESC
                LIMIT 100
                """,
                (game_mode,),
            )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        release_conn(conn)

    results = [
        ScoreResponse(
            id=row[0],
            player=row[1],
            score=row[2],
            game_mode=row[3],
            submitted_at=row[4].replace(tzinfo=timezone.utc).isoformat(),
        )
        for row in rows
    ]

    # Attempt cache write — non-fatal if Redis is unavailable
    try:
        get_cache().setex(
            f"{CACHE_KEY_PREFIX}{game_mode}",
            CACHE_TTL,
            json.dumps([r.model_dump() for r in results]),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return results

@router.get("/scores/all", response_model=list[ScoreResponse])
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
                ORDER BY score DESC
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