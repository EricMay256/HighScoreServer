import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from app.models import LeaderboardResponse, ScoreSubmission, ScoreResponse, GameModeConfig, GameModeCreate
from app.db import get_conn, release_conn
from app.cache import get_cache
from app.dependencies import require_api_key, require_user
from app.periods import get_period_start, PERIODS
from psycopg2 import errors as pg_errors
from app.limiter import limiter
from starlette.requests import Request

router = APIRouter(tags=["leaderboard"])
logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "leaderboard:"
CACHE_TTL = 120  # seconds

@router.get("/game_modes", response_model=list[GameModeConfig])
@limiter.limit("60/minute")
def list_game_modes(request: Request) -> list[GameModeConfig]:
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
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    sort_order = EXCLUDED.sort_order,
                    label      = EXCLUDED.label,
                    requires_auth = EXCLUDED.requires_auth
                RETURNING name, sort_order, label, requires_auth
                """,
                (config.name, config.sort_order, config.label, config.requires_auth),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return GameModeConfig(name=row[0], sort_order=row[1], label=row[2], requires_auth=row[3])

@router.get("/latest", response_model=list[ScoreResponse])
@limiter.limit("10/minute")
def latest_scores(request: Request) -> list[ScoreResponse]:
    # Attempt cache read — fall through to DB if Redis is unavailable
    try:
        cache = get_cache()
        cached = cache.get(f"{CACHE_KEY_PREFIX}latest")
        if cached:
            return [ScoreResponse(**s) for s in json.loads(cached)]
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, u.username, s.score, s.game_mode, s.submitted_at
                FROM leaderboard_snapshots s
                JOIN users u ON u.id = s.user_id
                ORDER BY s.submitted_at DESC
                LIMIT 100
                """
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
            submitted_at=row[4].astimezone(timezone.utc).isoformat(),
        )
        for row in rows
    ]

    try:
        get_cache().setex(
            f"{CACHE_KEY_PREFIX}latest",
            CACHE_TTL,
            json.dumps([s.model_dump() for s in results]),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return results

@router.get("/scores", response_model=LeaderboardResponse)
@limiter.limit("60/minute")
def get_scores(request: Request, game_mode: str, period: str = "alltime") -> LeaderboardResponse:
    cache_key = f"{CACHE_KEY_PREFIX}{game_mode}:{period}"
    try:
        cache = get_cache()
        cached = cache.get(cache_key)
        if cached:
            return LeaderboardResponse(**json.loads(cached))
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    if period not in PERIODS:
        allowed_periods = ", ".join(PERIODS)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid period: {period}. Allowed values: {allowed_periods}",
        )
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
                            SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                            RANK() OVER (ORDER BY s.score {order}, s.submitted_at ASC, s.id ASC) AS rank,
                            COUNT(*) OVER() AS total_count
                            FROM leaderboard_snapshots s
                            JOIN users u ON u.id = s.user_id
                            WHERE s.game_mode = %s
                              AND s.period = %s
                              AND s.period_start = %s
                            ORDER BY s.score {order}, s.submitted_at ASC, s.id ASC
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

    total_count = rows[0][7] if rows else 0

    results = [
        ScoreResponse(
            id=row[0], player=row[1], score=row[2],
            game_mode=row[3],period=row[4],
            submitted_at=row[5].astimezone(timezone.utc).isoformat(),
            rank=row[6],
            percentile=round((1 - (row[6] - 1) / row[7]) * 100, 2) if row[7] > 1 else 100.0
        )
        for row in rows
    ]

    try:
        get_cache().setex(
            cache_key,
            CACHE_TTL,
            json.dumps(LeaderboardResponse(scores=results, total_count=total_count).model_dump()),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return LeaderboardResponse(scores=results, total_count=total_count)

@router.post(
    "/scores",
    response_model=ScoreResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("10/minute")
def submit_score(
    request:    Request,
    submission: ScoreSubmission,
    payload:    dict = Depends(require_user),
) -> ScoreResponse:
    user_id  = int(payload["sub"])
    is_guest = payload["is_guest"]

    conn = get_conn()
    now  = datetime.now(timezone.utc)

    try:
        with conn.cursor() as cur:
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
                        (score, game_mode, period, period_start, submitted_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, game_mode, period, period_start)
                    DO UPDATE SET
                        score        = EXCLUDED.score,
                        submitted_at = NOW()
                    WHERE { _is_improvement_predicate(order) }
                    RETURNING id, score, game_mode, period, submitted_at
                    """,
                    (submission.score, submission.game_mode,
                    period, period_start, now, user_id),
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

    result = _fetch_score_with_rank(user_id, submission.game_mode, "alltime")
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found after insertion, this should not happen",
        )
    return result

def _fetch_score_with_rank(user_id: int, game_mode: str, period: str = "alltime") -> ScoreResponse | None:
    # 
    """Fetch a single player's score with rank and percentile computed server-side.
    
    period is assumed to be a valid PERIODS value; callers responsible for validation"""
    period_start = get_period_start(period)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sort_order FROM game_modes WHERE name = %s", (game_mode,)
            )
            mode_row = cur.fetchone()
            if mode_row is None:
                return None
            order = "ASC" if mode_row[0] == "ASC" else "DESC"

            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT
                        s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                        s.user_id,
                        RANK()  OVER (ORDER BY score {order}, s.submitted_at ASC, s.id ASC) AS rank,
                        COUNT(*) OVER ()                                                AS total_count
                    FROM leaderboard_snapshots s
                    JOIN users u ON u.id = s.user_id
                    WHERE game_mode    = %s
                      AND period       = %s
                      AND period_start = %s
                )
                SELECT id, username, score, game_mode, period, submitted_at, rank, total_count
                FROM ranked
                WHERE user_id = %s
                LIMIT 1
                """,
                (game_mode, period, period_start, user_id),
            )
            row = cur.fetchone()
    finally:
        release_conn(conn)

    if row is None:
        return None

    total = row[7]
    return ScoreResponse(
        id=row[0], player=row[1], score=row[2],
        game_mode=row[3], period=row[4],
        submitted_at=row[5].astimezone(timezone.utc).isoformat(),
        rank=row[6],
        percentile=round((1 - (row[6] - 1) / total) * 100, 2) if total > 1 else 100.0,
    )

def _is_improvement_predicate(order: str) -> str:
    # Returns a SQL fragment: true when EXCLUDED.score is better than stored score
    # ASC = lower score is better (ie race time)
    # DESC = higher score is better (ie points).
    # Update scores when new score "beats" old score 
    # (new < stored for ASC, new > stored for DESC)
    if order == "ASC":
        return "EXCLUDED.score < leaderboard_snapshots.score"
    else:
        return "EXCLUDED.score > leaderboard_snapshots.score"