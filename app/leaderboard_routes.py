import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from app.models import ScoreSubmission, ScoreResponse, GameModeConfig, GameModeCreate
from app.db import get_conn, release_conn
from app.cache import get_cache
from app.dependencies import require_api_key, require_user
from app.periods import get_period_start, PERIODS
from psycopg2 import errors as pg_errors

router = APIRouter(tags=["leaderboard"])
logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "leaderboard:"
CACHE_TTL = 120  # seconds

@router.get("/game_modes", response_model=list[GameModeConfig])
def list_game_modes() -> list[GameModeConfig]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, sort_order, label, requires_auth FROM game_modes ORDER BY name")
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return [GameModeConfig(name=r[0], sort_order=r[1], label=r[2], requires_auth=r[3]) for r in rows]


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
                INSERT INTO game_modes (name, sort_order, label, requires_auth)
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
                FROM leaderboard_snapshots
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
def get_scores(game_mode: str, period: str = "alltime") -> list[ScoreResponse]:
    try:
        cache = get_cache()
        cache_key = f"{CACHE_KEY_PREFIX}{game_mode}:{period}"
        cached = cache.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    period_start = get_period_start(period)

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
                SELECT id, player, score, game_mode, period, submitted_at
                FROM leaderboard_snapshots
                WHERE game_mode = %s
                  AND period = %s
                  AND period_start = %s
                ORDER BY score {order}, submitted_at ASC, id ASC
                LIMIT 100
                """,
                (game_mode, period, period_start),
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
            game_mode=row[3],period=row[4],
            submitted_at=row[5].replace(tzinfo=timezone.utc).isoformat(),
        )
        for row in rows
    ]

    try:
        get_cache().setex(
            cache_key,
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
)
def submit_score(
    submission: ScoreSubmission,
    payload:    dict = Depends(require_user),
) -> ScoreResponse:
    user_id  = int(payload["sub"])
    username = payload["username"]
    is_guest = payload.get("is_guest", False)

    conn = get_conn()
    now  = datetime.now(timezone.utc)

    try:
        with conn.cursor() as cur:
            last_result: ScoreResponse | None = None

            cur.execute(
                "SELECT sort_order, requires_auth FROM game_modes WHERE name = %s",
                (submission.game_mode,),
            )
            mode_row = cur.fetchone()
            if mode_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Unknown game mode: {submission.game_mode}",
                )

            sort_order, requires_auth = mode_row

            if requires_auth and is_guest:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This game mode requires a claimed account",
                )

            order = "ASC" if sort_order == "ASC" else "DESC"

            for period in PERIODS:
                period_start = get_period_start(period, at=now)

                cur.execute(
                    f"""
                    INSERT INTO leaderboard_snapshots
                        (player, score, game_mode, period, period_start, submitted_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (player, game_mode, period, period_start)
                    DO UPDATE SET
                        score        = EXCLUDED.score,
                        submitted_at = NOW()
                    WHERE {"leaderboard_snapshots.score > EXCLUDED.score" if order == "ASC" else "leaderboard_snapshots.score < EXCLUDED.score"}
                    RETURNING id, player, score, game_mode, period, submitted_at
                    """,
                    (username, submission.score, submission.game_mode,
                     period, period_start, now, user_id),
                )
                row = cur.fetchone()

                if row and period == "alltime":
                    last_result = ScoreResponse(
                        id=row[0], player=row[1], score=row[2],
                        game_mode=row[3], period=row[4],
                        submitted_at=row[5].replace(tzinfo=timezone.utc).isoformat(),
                    )

            conn.commit()
    except HTTPException:
        raise
    except pg_errors.ForeignKeyViolation:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid game mode: {submission.game_mode}",
        )
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    try:
        cache = get_cache()
        for period in PERIODS:
            cache.delete(f"{CACHE_KEY_PREFIX}{submission.game_mode}:{period}")
    except Exception as e:
        logger.warning("Redis cache invalidation failed, continuing: %s", e)

    if last_result is None:
        scores = get_scores(submission.game_mode, "alltime")
        match  = next((s for s in scores if s.player == username), None)
        if match:
            return ScoreResponse(**match.model_dump())
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found after insertion, this should not happen",
        )
    return last_result
