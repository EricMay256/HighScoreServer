import gzip
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response, Query, status
from app.models import (
    LeaderboardResponse, ScoreSubmission, ScoreResponse, GameModeConfig,
    GameModeCreate, RunSubmission,
)
from app.db import get_conn, release_conn
from app.cache import get_cache
from app.dependencies import require_api_key, require_user
from app.periods import get_period_start, PERIODS
from app.validation import RunRecord, default_validator
import psycopg2
from psycopg2 import errors as pg_errors
from app.limiter import limiter, rate_limited_responses
from starlette.requests import Request

router = APIRouter(tags=["leaderboard"])
logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "leaderboard:"
CACHE_TTL = 120  # seconds


class CrossRouteError(Exception):
    """Raised when a submission hits the wrong endpoint for its mode's tier.

    Handled globally (see app/main.py) into a 409 whose body keeps `detail` a
    human-readable string with machine-routable `code` / `submit_to` as
    top-level siblings — the shape the hss-unity client parses cleanly.
    """

    def __init__(self, code: str, submit_to: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.submit_to = submit_to
        self.detail = detail


# Rate limit for /runs — deliberately tighter than /scores (10/min) because
# validation is the expensive path. A chosen value, not a derived one; tune if
# real run traffic warrants.
RUNS_RATE_LIMIT = "5/minute"

@router.get("/game_modes", response_model=list[GameModeConfig], responses=rate_limited_responses("60 per minute"))
@limiter.limit("60/minute")
def list_game_modes(request: Request, response: Response) -> list[GameModeConfig]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, sort_order, label, requires_claimed_account,
                       required_tier, scoring_strategy, game_key
                FROM game_modes ORDER BY name
                """
            )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return [
        GameModeConfig(
            name=r[0], sort_order=r[1], label=r[2], requires_claimed_account=r[3],
            required_tier=r[4], scoring_strategy=r[5], game_key=r[6],
        )
        for r in rows
    ]


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
                INSERT INTO game_modes
                    (name, sort_order, label, requires_claimed_account,
                     required_tier, scoring_strategy, game_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    sort_order = EXCLUDED.sort_order,
                    label      = EXCLUDED.label,
                    requires_claimed_account = EXCLUDED.requires_claimed_account,
                    required_tier    = EXCLUDED.required_tier,
                    scoring_strategy = EXCLUDED.scoring_strategy,
                    game_key         = EXCLUDED.game_key
                RETURNING name, sort_order, label, requires_claimed_account,
                          required_tier, scoring_strategy, game_key
                """,
                (config.name, config.sort_order, config.label,
                 config.requires_claimed_account, config.required_tier,
                 config.scoring_strategy, config.game_key),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return GameModeConfig(
        name=row[0], sort_order=row[1], label=row[2], requires_claimed_account=row[3],
        required_tier=row[4], scoring_strategy=row[5], game_key=row[6],
    )

@router.get("/latest", response_model=LeaderboardResponse, responses=rate_limited_responses("10 per minute"))
@limiter.limit("10/minute")
def latest_scores(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    game_modes: list[str] | None = Query(None, max_length=10),
) -> LeaderboardResponse:
    if game_modes is not None:
        # Per-mode length validation matches ScoreSubmission.game_mode (1..32).
        for mode in game_modes:
            if not 1 <= len(mode) <= 32:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Each game_mode must be 1..32 characters",
                )
        # Dedupe + sort: canonical form for cache-key stability and SQL hygiene.
        game_modes = sorted(set(game_modes))
    modes_key = ",".join(game_modes) if game_modes else "all"
    cache_key = f"{CACHE_KEY_PREFIX}latest:{modes_key}:{limit}:{offset}"
    try:
        cache = get_cache()
        cached = cache.get(cache_key)
        if cached:
            return LeaderboardResponse(**json.loads(cached))
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # COUNT(*) OVER () gives total scores across the whole table.
            # Unlike /scores this isn't filtered to a mode/period — the "latest"
            # feed is global. Worth knowing if the table grows large; the count
            # is cheap on indexed columns but not free.
            if game_modes:
                cur.execute(
                    """
                    SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                        COUNT(*) OVER() AS total_count
                    FROM scores s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.game_mode = ANY(%s)
                    AND s.period = 'alltime'
                    ORDER BY s.submitted_at DESC, s.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (game_modes, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                        COUNT(*) OVER() AS total_count
                    FROM scores s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.period = 'alltime'
                    ORDER BY s.submitted_at DESC, s.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        release_conn(conn)

    if rows:
        total_count = rows[0][6]
    elif offset > 0:
        total_count = _count_all_scores()
    else:
        total_count = 0

    results = [
        ScoreResponse(
            id=row[0],
            player=row[1],
            score=row[2],
            game_mode=row[3],
            period=row[4],
            submitted_at=row[5].astimezone(timezone.utc).isoformat(),
        )
        for row in rows
    ]

    response = LeaderboardResponse(scores=results, total_count=total_count)

    try:
        get_cache().setex(
            cache_key,
            CACHE_TTL,
            json.dumps(response.model_dump()),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return response

@router.get("/scores", response_model=LeaderboardResponse, responses=rate_limited_responses("60 per minute"))
@limiter.limit("60/minute")
def get_scores(
    request: Request,
    response: Response,
    game_mode: str,
    period: str = "alltime",
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> LeaderboardResponse:
    cache_key = f"{CACHE_KEY_PREFIX}{game_mode}:{period}:{limit}:{offset}"
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

            # RANK() and COUNT(*) OVER () are computed over the full filtered set
            # *before* LIMIT/OFFSET — so rank and total_count remain correct
            # regardless of the page being requested. This is the key reason
            # offset pagination composes cleanly with this query shape.
            cur.execute(
                f"""
                SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                RANK() OVER (ORDER BY s.score {order}, s.submitted_at ASC, s.id ASC) AS rank,
                COUNT(*) OVER() AS total_count
                FROM scores s
                JOIN users u ON u.id = s.user_id
                WHERE s.game_mode = %s
                  AND s.period = %s
                  AND s.period_start = %s
                ORDER BY s.score {order}, s.submitted_at ASC, s.id ASC
                LIMIT %s OFFSET %s
                """,
                (game_mode, period, period_start, limit, offset),
            )
            rows = cur.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    # total_count from the window function is only present on returned rows.
    # If the page is empty (offset past end, or no scores at all), fall back
    # to a separate COUNT — but only when offset > 0, since a truly empty
    # leaderboard should report 0 without a second query.
    if rows:
        total_count = rows[0][7]
    elif offset > 0:
        total_count = _count_scores(game_mode, period, period_start)
    else:
        total_count = 0

    results = [
        ScoreResponse(
            id=row[0], player=row[1], score=row[2],
            game_mode=row[3], period=row[4],
            submitted_at=row[5].astimezone(timezone.utc).isoformat(),
            rank=row[6],
            percentile=round((1 - (row[6] - 1) / row[7]) * 100, 2) if row[7] > 1 else 100.0,
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
    responses=rate_limited_responses("10 per minute"),
)
@limiter.limit("10/minute")
def submit_score(
    request:    Request,
    response:   Response,
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
                """
                SELECT sort_order, requires_claimed_account, scoring_strategy, required_tier
                FROM game_modes WHERE name = %s
                """,
                (submission.game_mode,),
            )
            mode_row = cur.fetchone()
            if mode_row is None:
                # Raises without rolling back transaction - OK since no modifications made.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Unknown game mode: {submission.game_mode}",
                )

            sort_order, requires_claimed_account, scoring_strategy, required_tier = mode_row

            # Run-required modes don't accept raw scores — guide the client to /runs.
            if required_tier > 0:
                raise CrossRouteError(
                    code="RUN_REQUIRED",
                    submit_to="/api/leaderboard/runs",
                    detail="This game mode requires a validated run; submit to /api/leaderboard/runs",
                )

            if requires_claimed_account and is_guest:
                # Raises without rolling back transaction - OK since no modifications made.
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This game mode requires a claimed account",
                )

            # Cumulative modes dedup increments by idempotency key, so the key
            # is mandatory there. The requirement is data-dependent (it hinges
            # on the looked-up mode), hence validated here, not in the model.
            if scoring_strategy == "cumulative" and submission.idempotency_key is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="idempotency_key is required for cumulative game modes",
                )

            order = "ASC" if sort_order == "ASC" else "DESC"

            _apply_score_write(
                cur,
                user_id=user_id,
                game_mode=submission.game_mode,
                score=submission.score,
                order=order,
                scoring_strategy=scoring_strategy,
                now=now,
                run_id=None,
                idempotency_key=submission.idempotency_key,
            )

            conn.commit()
    except (HTTPException, CrossRouteError):
        raise
    except pg_errors.ForeignKeyViolation:
        # Shouldn't be reachable: game_mode is validated prior to upsert,
        # and user_id comes from a verified JWT. Kept for redundancy.
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

    _invalidate_score_caches(submission.game_mode)

    result = _fetch_score_with_rank(user_id, submission.game_mode, "alltime")
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found after insertion, this should not happen",
        )
    return result


@router.post(
    "/runs",
    response_model=ScoreResponse,
    status_code=status.HTTP_201_CREATED,
    responses=rate_limited_responses(RUNS_RATE_LIMIT),
)
@limiter.limit(RUNS_RATE_LIMIT)
def submit_run(
    request:    Request,
    response:   Response,
    submission: RunSubmission,
    payload:    dict = Depends(require_user),
) -> ScoreResponse:
    """Submit a full run for server-side validation.

    The client's claimed_score is recorded but never authoritative — the
    server computes the canonical score via the validator and upserts that.
    Anti-replay/idempotency is the runs UNIQUE(user_id, game_mode, client_run_id):
    a resubmitted run returns its prior result without re-validating.
    """
    user_id  = int(payload["sub"])
    is_guest = payload["is_guest"]

    conn = get_conn()
    now  = datetime.now(timezone.utc)
    validation_tier = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sort_order, requires_claimed_account, scoring_strategy, required_tier
                FROM game_modes WHERE name = %s
                """,
                (submission.game_mode,),
            )
            mode_row = cur.fetchone()
            if mode_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Unknown game mode: {submission.game_mode}",
                )

            sort_order, requires_claimed_account, scoring_strategy, required_tier = mode_row

            # Raw-only modes don't validate runs — guide the client to /scores.
            if required_tier < 1:
                raise CrossRouteError(
                    code="RAW_ONLY",
                    submit_to="/api/leaderboard/scores",
                    detail="This game mode accepts raw scores; submit to /api/leaderboard/scores",
                )

            if requires_claimed_account and is_guest:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This game mode requires a claimed account",
                )

            # Anti-replay: a prior run with this client_run_id returns its result
            # without re-validating.
            cur.execute(
                """
                SELECT status, validation_tier FROM runs
                WHERE user_id = %s AND game_mode = %s AND client_run_id = %s
                """,
                (user_id, submission.game_mode, submission.client_run_id),
            )
            prior = cur.fetchone()
            if prior is not None:
                conn.rollback()  # nothing to write; release the read transaction
                return _existing_run_response(prior, user_id, submission.game_mode)

            # Persist the run as pending. The action log is stored as a single
            # gzipped JSON blob (not a normalized table) per ADR/Heroku economics.
            actions_blob = gzip.compress(json.dumps(submission.actions).encode("utf-8"))
            cur.execute(
                """
                INSERT INTO runs
                    (user_id, game_mode, scenario_version, seed, claimed_score,
                     client_run_id, actions, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (user_id, submission.game_mode, submission.scenario_version,
                 submission.seed, submission.claimed_score, submission.client_run_id,
                 psycopg2.Binary(actions_blob)),
            )
            run_id = cur.fetchone()[0]

            # Validate. The validator must achieve at least the mode's required
            # tier; it records the tier actually achieved.
            record = RunRecord(
                id=run_id,
                user_id=user_id,
                game_mode=submission.game_mode,
                scenario_version=submission.scenario_version,
                seed=submission.seed,
                actions=submission.actions,
                claimed_score=submission.claimed_score,
            )
            result = default_validator.validate(record, required_tier)
            validation_tier = result.tier_achieved

            if result.status == "rejected":
                cur.execute(
                    "UPDATE runs SET status = 'rejected', validation_tier = %s WHERE id = %s",
                    (result.tier_achieved, run_id),
                )
                conn.commit()
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Run rejected: {result.reason}",
                )

            # Validated: record the server-computed canonical score + tier, then
            # write it to the leaderboard linked to this run.
            cur.execute(
                """
                UPDATE runs SET status = 'validated', canonical_score = %s,
                    validation_tier = %s WHERE id = %s
                """,
                (result.canonical_score, result.tier_achieved, run_id),
            )
            order = "ASC" if sort_order == "ASC" else "DESC"
            _apply_score_write(
                cur,
                user_id=user_id,
                game_mode=submission.game_mode,
                score=result.canonical_score,
                order=order,
                scoring_strategy=scoring_strategy,
                now=now,
                run_id=run_id,
                # runs.client_run_id already gave anti-replay above, so don't
                # double-write submission_idempotency for cumulative run modes.
                record_idempotency=False,
            )
            conn.commit()
    except (HTTPException, CrossRouteError):
        raise
    except pg_errors.UniqueViolation:
        # Lost a race on client_run_id — treat as a duplicate submission.
        conn.rollback()
        existing = _lookup_run(user_id, submission.game_mode, submission.client_run_id)
        if existing is not None:
            return _existing_run_response(existing, user_id, submission.game_mode)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate run submission",
        )
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    _invalidate_score_caches(submission.game_mode)

    result_resp = _fetch_score_with_rank(user_id, submission.game_mode, "alltime")
    if result_resp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found after run validation, this should not happen",
        )
    result_resp.validated = True
    result_resp.validation_tier = validation_tier
    return result_resp


def _invalidate_score_caches(game_mode: str) -> None:
    """Invalidate every cached read variant touched by a write to ``game_mode``.

    /scores keys are ``leaderboard:{mode}:{period}:{limit}:{offset}`` (deleted by
    mode prefix); /latest keys are ``leaderboard:latest:{modes}:{limit}:{offset}``
    (deleted by the ``latest:`` prefix, which catches every mode subset).
    """
    try:
        cache = get_cache()
        cache.delete_prefix(f"{CACHE_KEY_PREFIX}{game_mode}:")
        cache.delete_prefix(f"{CACHE_KEY_PREFIX}latest:")
    except Exception as e:
        logger.warning("Cache invalidation failed, continuing: %s", e)


def _lookup_run(user_id: int, game_mode: str, client_run_id: str):
    """Fetch (status, validation_tier) for an existing run, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, validation_tier FROM runs
                WHERE user_id = %s AND game_mode = %s AND client_run_id = %s
                """,
                (user_id, game_mode, client_run_id),
            )
            return cur.fetchone()
    finally:
        release_conn(conn)


def _existing_run_response(prior, user_id: int, game_mode: str) -> ScoreResponse:
    """Return the prior result for a replayed run without re-validating.

    ``prior`` is the (status, validation_tier) row. A validated run returns the
    player's current standing flagged validated; a rejected/pending run can't
    produce a score, so it surfaces the prior outcome as an error.
    """
    prior_status, prior_tier = prior
    if prior_status == "validated":
        resp = _fetch_score_with_rank(user_id, game_mode, "alltime")
        if resp is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Score not found for prior validated run",
            )
        resp.validated = True
        resp.validation_tier = prior_tier if prior_tier is not None else 0
        return resp
    if prior_status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Run was previously rejected",
        )
    # 'pending' — only reachable if an earlier attempt persisted but didn't finish.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Run is already submitted and pending",
    )


def _count_scores(game_mode: str, period: str, period_start) -> int:
    """Count scores for a given mode/period bucket. Used when a paginated
    response returns an empty page but the leaderboard isn't actually empty."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM scores
                WHERE game_mode = %s AND period = %s AND period_start = %s
                """,
                (game_mode, period, period_start),
            )
            return cur.fetchone()[0]
    finally:
        release_conn(conn)

def _count_all_scores() -> int:
    """Total row count for the scores table. Used by the /latest endpoint
    to report total_count when a paginated request lands on an empty page."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM scores")
            return cur.fetchone()[0]
    finally:
        release_conn(conn)

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
                    FROM scores s
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

def _apply_score_write(
    cur,
    *,
    user_id: int,
    game_mode: str,
    score: int,
    order: str,
    scoring_strategy: str,
    now: datetime,
    run_id: int | None = None,
    idempotency_key: str | None = None,
    record_idempotency: bool = True,
) -> None:
    """Per-period score write inside the caller's open transaction (no commit).

    Single source of truth for write semantics, shared by /scores and /runs.
    The caller is responsible for the surrounding transaction, mode validation,
    cache invalidation, and building the response.

    - ``best``       — improvement-gated upsert; ``score`` replaces the stored
      best when it beats it (per ``order``), else the row is left untouched.
    - ``cumulative`` — additive upsert; ``score`` is the *increment* for this
      submission, applied to every period bucket with no improvement gate.

    Cumulative dedup: on the raw (/scores) path, ``record_idempotency=True``
    inserts a ``submission_idempotency`` marker first and a conflict (duplicate
    key) makes the whole write a no-op. On the /runs path, ``runs.client_run_id``
    UNIQUE already provides anti-replay (the endpoint returns the prior result
    before reaching here), so callers pass ``record_idempotency=False`` to avoid
    a redundant second dedup write — resolving the open "reuse runs.client_run_id
    vs. unified table" question in favor of not double-writing.

    ``order`` and ``scoring_strategy`` are DB-sourced / CHECK-constrained, never
    raw user input — preserving the SQL-interpolation invariant.
    """
    if scoring_strategy == "cumulative":
        if record_idempotency:
            # Idempotency gate: one marker per (user, mode, key) gates the
            # increment across all period buckets atomically in this transaction.
            cur.execute(
                """
                INSERT INTO submission_idempotency (user_id, game_mode, key)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user_id, game_mode, idempotency_key),
            )
            if cur.rowcount == 0:
                return  # duplicate submission — no-op, leave totals unchanged

        for period in PERIODS:
            period_start = get_period_start(period, at=now)
            cur.execute(
                """
                INSERT INTO scores
                    (score, game_mode, period, period_start, submitted_at, user_id, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, game_mode, period, period_start)
                DO UPDATE SET
                    score        = scores.score + EXCLUDED.score,
                    submitted_at = NOW(),
                    run_id       = EXCLUDED.run_id
                """,
                (score, game_mode, period, period_start, now, user_id, run_id),
            )
        return

    # best (default): improvement-gated upsert.
    for period in PERIODS:
        period_start = get_period_start(period, at=now)
        # order is DB-sourced, CHECK-constrained to 'ASC'|'DESC'.
        cur.execute(
            f"""
            INSERT INTO scores
                (score, game_mode, period, period_start, submitted_at, user_id, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, game_mode, period, period_start)
            DO UPDATE SET
                score        = EXCLUDED.score,
                submitted_at = NOW(),
                run_id       = EXCLUDED.run_id
            WHERE { _is_improvement_predicate(order) }
            """,
            (score, game_mode, period, period_start, now, user_id, run_id),
        )


def _is_improvement_predicate(order: str) -> str:
    # Returns a SQL fragment: true when EXCLUDED.score is better than stored score
    # ASC = lower score is better (ie race time)
    # DESC = higher score is better (ie points).
    # Update scores when new score "beats" old score 
    # (new < stored for ASC, new > stored for DESC)
    if order == "ASC":
        return "EXCLUDED.score < scores.score"
    else:
        return "EXCLUDED.score > scores.score"