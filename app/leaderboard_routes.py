import gzip
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg import errors as pg_errors
from starlette.requests import Request

from app.cache import get_cache
from app.db import get_pool
from app.dependencies import require_api_key, require_user
from app.limiter import limiter, rate_limited_responses
from app.models import (
    MAX_SCORE,
    GameModeConfig,
    GameModeCreate,
    LeaderboardResponse,
    RunSubmission,
    ScoreResponse,
    ScoreSubmission,
)
from app.periods import PERIODS, get_period_start
from app.validation import ModeBounds, RunRecord, default_validator


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
async def list_game_modes(request: Request, response: Response) -> list[GameModeConfig]:
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT name, sort_order, label, requires_claimed_account,
                           required_tier, scoring_strategy, game_key, max_score
                    FROM game_modes ORDER BY name
                    """
                )
                rows = await cur.fetchall()
    except Exception as e:
        logger.error("Game mode listing error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    return [
        GameModeConfig(
            name=r[0], sort_order=r[1], label=r[2], requires_claimed_account=r[3],
            required_tier=r[4], scoring_strategy=r[5], game_key=r[6], max_score=r[7],
        )
        for r in rows
    ]


@router.post(
    "/game_modes",
    response_model=GameModeConfig,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_game_mode(config: GameModeCreate) -> GameModeConfig:
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO game_modes
                        (name, sort_order, label, requires_claimed_account,
                         required_tier, scoring_strategy, game_key, max_score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        sort_order = EXCLUDED.sort_order,
                        label      = EXCLUDED.label,
                        requires_claimed_account = EXCLUDED.requires_claimed_account,
                        required_tier    = EXCLUDED.required_tier,
                        scoring_strategy = EXCLUDED.scoring_strategy,
                        game_key         = EXCLUDED.game_key,
                        max_score        = EXCLUDED.max_score
                    RETURNING name, sort_order, label, requires_claimed_account,
                              required_tier, scoring_strategy, game_key, max_score
                    """,
                    (config.name, config.sort_order, config.label,
                     config.requires_claimed_account, config.required_tier,
                     config.scoring_strategy, config.game_key, config.max_score),
                )
                row = await cur.fetchone()
            # connection context manager commits on clean exit
    except Exception as e:
        logger.error("Game mode creation error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    return GameModeConfig(
        name=row[0], sort_order=row[1], label=row[2], requires_claimed_account=row[3],
        required_tier=row[4], scoring_strategy=row[5], game_key=row[6], max_score=row[7],
    )

@router.get("/latest", response_model=LeaderboardResponse, responses=rate_limited_responses("10 per minute"))
@limiter.limit("10/minute")
async def latest_scores(
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
        cached = await cache.get(cache_key)
        if cached:
            return LeaderboardResponse(**json.loads(cached))
    except Exception as e:
        logger.warning("Redis read failed, falling back to DB: %s", e)

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                # COUNT(*) OVER () counts the rows this query selects, so it
                # already respects both the 'alltime' period and any game_modes
                # filter. _count_latest_scores must repeat those predicates.
                # Cheap on indexed columns, but not free as the table grows.
                # validation_tier read straight off scores (denormalized) — no join.
                if game_modes:
                    await cur.execute(
                        """
                        SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                            COUNT(*) OVER() AS total_count,
                            s.validation_tier
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
                    await cur.execute(
                        """
                        SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                            COUNT(*) OVER() AS total_count,
                            s.validation_tier
                        FROM scores s
                        JOIN users u ON u.id = s.user_id
                        WHERE s.period = 'alltime'
                        ORDER BY s.submitted_at DESC, s.id DESC
                        LIMIT %s OFFSET %s
                        """,
                        (limit, offset),
                    )
                rows = await cur.fetchall()
    except Exception as e:
        logger.error("Latest scores error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if rows:
        total_count = rows[0][6]
    elif offset > 0:
        total_count = await _count_latest_scores(game_modes)
    else:
        total_count = 0

    results = [
        ScoreResponse(
            id=row[0],
            player=row[1],
            score=row[2],
            game_mode=row[3],
            period=row[4],
            submitted_at=row[5].astimezone(UTC).isoformat(),
            validated=row[7] > 0,
            validation_tier=row[7],
        )
        for row in rows
    ]

    response = LeaderboardResponse(scores=results, total_count=total_count)

    try:
        await get_cache().setex(
            cache_key,
            CACHE_TTL,
            json.dumps(response.model_dump()),
        )
    except Exception as e:
        logger.warning("Redis write failed, continuing without cache: %s", e)

    return response

@router.get("/scores", response_model=LeaderboardResponse, responses=rate_limited_responses("60 per minute"))
@limiter.limit("60/minute")
async def get_scores(
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
        cached = await cache.get(cache_key)
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

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT sort_order FROM game_modes WHERE name = %s",
                    (game_mode,),
                )
                mode_row = await cur.fetchone()
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
                # validation_tier is denormalized onto scores at write time, so the
                # hot read needs no join to runs — index-friendly. validated is
                # derived from tier > 0.
                await cur.execute(
                    f"""
                    SELECT s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                    RANK() OVER (ORDER BY s.score {order}, s.submitted_at ASC, s.id ASC) AS rank,
                    COUNT(*) OVER() AS total_count,
                    s.validation_tier
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
                rows = await cur.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Score listing error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    # total_count from the window function is only present on returned rows.
    # If the page is empty (offset past end, or no scores at all), fall back
    # to a separate COUNT — but only when offset > 0, since a truly empty
    # leaderboard should report 0 without a second query.
    if rows:
        total_count = rows[0][7]
    elif offset > 0:
        total_count = await _count_scores(game_mode, period, period_start)
    else:
        total_count = 0

    results = [
        ScoreResponse(
            id=row[0], player=row[1], score=row[2],
            game_mode=row[3], period=row[4],
            submitted_at=row[5].astimezone(UTC).isoformat(),
            rank=row[6],
            percentile=round((1 - (row[6] - 1) / row[7]) * 100, 2) if row[7] > 1 else 100.0,
            validated=row[8] > 0,
            validation_tier=row[8],
        )
        for row in rows
    ]

    try:
        await get_cache().setex(
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
async def submit_score(
    request:    Request,
    response:   Response,
    submission: ScoreSubmission,
    payload:    dict = Depends(require_user),
) -> ScoreResponse:
    user_id  = int(payload["sub"])
    is_guest = payload["is_guest"]

    now  = datetime.now(UTC)

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT sort_order, requires_claimed_account, scoring_strategy,
                           required_tier, max_score
                    FROM game_modes WHERE name = %s
                    """,
                    (submission.game_mode,),
                )
                mode_row = await cur.fetchone()
                if mode_row is None:
                    # Raises without writing; the connection CM rolls back the empty txn.
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Unknown game mode: {submission.game_mode}",
                    )

                (sort_order, requires_claimed_account, scoring_strategy,
                 required_tier, max_score) = mode_row

                # Run-required modes don't accept raw scores — guide the client to /runs.
                if required_tier > 0:
                    raise CrossRouteError(
                        code="RUN_REQUIRED",
                        submit_to="/api/leaderboard/runs",
                        detail="This game mode requires a validated run; submit to /api/leaderboard/runs",
                    )

                # Tier 0 is unvalidated, but the per-mode ceiling still applies:
                # compare the submitted score directly to the mode's max_score
                # (NULL inherits the global MAX_SCORE, which the model already
                # enforces — so this only bites when a mode sets a tighter cap).
                score_ceiling = max_score if max_score is not None else MAX_SCORE
                if submission.score > score_ceiling:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Invalid Score",
                    )

                if requires_claimed_account and is_guest:
                    # Raises without writing; the connection CM rolls back the empty txn.
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

                await _apply_score_write(
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
            # connection context manager commits on clean exit
    except (HTTPException, CrossRouteError):
        raise
    except pg_errors.ForeignKeyViolation:
        # Shouldn't be reachable: game_mode is validated prior to upsert,
        # and user_id comes from a verified JWT. Kept for redundancy.
        # The connection CM has already rolled back.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid game mode: {submission.game_mode}",
        ) from None
    except Exception as e:
        logger.error("Score submission error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    await _invalidate_score_caches(submission.game_mode)

    result = await _fetch_score_with_rank(user_id, submission.game_mode, "alltime")
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
async def submit_run(
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

    now  = datetime.now(UTC)

    # Set when an existing run is found inside the transaction; the prior-result
    # response is built after the connection is released (it opens its own).
    prior_status: str | None = None

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT sort_order, requires_claimed_account, scoring_strategy,
                           required_tier, max_score
                    FROM game_modes WHERE name = %s
                    """,
                    (submission.game_mode,),
                )
                mode_row = await cur.fetchone()
                if mode_row is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Unknown game mode: {submission.game_mode}",
                    )

                (sort_order, requires_claimed_account, scoring_strategy,
                 required_tier, max_score) = mode_row

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
                await cur.execute(
                    """
                    SELECT status FROM runs
                    WHERE user_id = %s AND game_mode = %s AND client_run_id = %s
                    """,
                    (user_id, submission.game_mode, submission.client_run_id),
                )
                prior = await cur.fetchone()
                if prior is not None:
                    # Nothing to write; the read txn is released by the CM. The
                    # prior-result response is built after exiting this block.
                    prior_status = prior[0]
                else:
                    # Persist the run as pending. The action log is stored as a single
                    # gzipped JSON blob (not a normalized table) per ADR/Heroku economics.
                    # psycopg3 adapts bytes to bytea directly — no Binary() wrapper.
                    actions_blob = gzip.compress(json.dumps(submission.actions).encode("utf-8"))
                    await cur.execute(
                        """
                        INSERT INTO runs
                            (user_id, game_mode, scenario_version, seed, claimed_score,
                             claimed_tier, client_run_id, actions, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                        RETURNING id
                        """,
                        (user_id, submission.game_mode, submission.scenario_version,
                         submission.seed, submission.claimed_score, submission.claimed_tier,
                         submission.client_run_id, actions_blob),
                    )
                    run_id = (await cur.fetchone())[0]

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
                    # The route reads the mode's config and builds the bounds; the
                    # validator never touches the DB. claimed_tier is recorded above
                    # but not trusted — validation targets the mode's required_tier.
                    bounds = ModeBounds(max_score=max_score)
                    result = default_validator.validate(record, required_tier, bounds)

                    if result.status == "rejected":
                        await cur.execute(
                            "UPDATE runs SET status = 'rejected', validation_tier = %s WHERE id = %s",
                            (result.tier_achieved, run_id),
                        )
                        # Persist the rejection explicitly before raising: raising
                        # inside the connection CM triggers a rollback, so an
                        # implicit commit would lose the 'rejected' status.
                        await conn.commit()
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=f"Run rejected: {result.reason}",
                        )

                    # Validated: record the server-computed canonical score + tier, then
                    # write it to the leaderboard linked to this run.
                    await cur.execute(
                        """
                        UPDATE runs SET status = 'validated', canonical_score = %s,
                            validation_tier = %s WHERE id = %s
                        """,
                        (result.canonical_score, result.tier_achieved, run_id),
                    )
                    order = "ASC" if sort_order == "ASC" else "DESC"
                    await _apply_score_write(
                        cur,
                        user_id=user_id,
                        game_mode=submission.game_mode,
                        score=result.canonical_score,
                        order=order,
                        scoring_strategy=scoring_strategy,
                        now=now,
                        run_id=run_id,
                        validation_tier=result.tier_achieved,
                        # runs.client_run_id already gave anti-replay above, so don't
                        # double-write submission_idempotency for cumulative run modes.
                        record_idempotency=False,
                    )
            # connection context manager commits the validated write on clean exit
    except (HTTPException, CrossRouteError):
        raise
    except pg_errors.UniqueViolation:
        # Lost a race on client_run_id — treat as a duplicate submission.
        # The connection CM has already rolled back.
        existing_status = await _lookup_run(user_id, submission.game_mode, submission.client_run_id)
        if existing_status is not None:
            return await _existing_run_response(existing_status, user_id, submission.game_mode)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate run submission",
        ) from None
    except Exception as e:
        logger.error("Run submission error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if prior_status is not None:
        return await _existing_run_response(prior_status, user_id, submission.game_mode)

    await _invalidate_score_caches(submission.game_mode)

    result_resp = await _fetch_score_with_rank(user_id, submission.game_mode, "alltime")
    if result_resp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found after run validation, this should not happen",
        )
    # validated/validation_tier come from the denormalized scores column via
    # _fetch — no manual override needed.
    return result_resp


async def _invalidate_score_caches(game_mode: str) -> None:
    """Invalidate every cached read variant touched by a write to ``game_mode``.

    /scores keys are ``leaderboard:{mode}:{period}:{limit}:{offset}`` (deleted by
    mode prefix); /latest keys are ``leaderboard:latest:{modes}:{limit}:{offset}``
    (deleted by the ``latest:`` prefix, which catches every mode subset).
    """
    try:
        cache = get_cache()
        await cache.delete_prefix(f"{CACHE_KEY_PREFIX}{game_mode}:")
        await cache.delete_prefix(f"{CACHE_KEY_PREFIX}latest:")
    except Exception as e:
        logger.warning("Cache invalidation failed, continuing: %s", e)


async def _lookup_run(user_id: int, game_mode: str, client_run_id: str) -> str | None:
    """Fetch the status of an existing run, or None."""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status FROM runs
                WHERE user_id = %s AND game_mode = %s AND client_run_id = %s
                """,
                (user_id, game_mode, client_run_id),
            )
            row = await cur.fetchone()
            return row[0] if row is not None else None


async def _existing_run_response(prior_status: str, user_id: int, game_mode: str) -> ScoreResponse:
    """Return the prior result for a replayed run without re-validating.

    A validated run returns the player's current standing (validated/tier come
    from the denormalized scores column via _fetch); a rejected/pending run
    can't produce a score, so it surfaces the prior outcome as an error.
    """
    if prior_status == "validated":
        resp = await _fetch_score_with_rank(user_id, game_mode, "alltime")
        if resp is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Score not found for prior validated run",
            )
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


async def _count_scores(game_mode: str, period: str, period_start) -> int:
    """Count scores for a given mode/period bucket. Used when a paginated
    response returns an empty page but the leaderboard isn't actually empty."""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) FROM scores
                WHERE game_mode = %s AND period = %s AND period_start = %s
                """,
                (game_mode, period, period_start),
            )
            return (await cur.fetchone())[0]

async def _count_latest_scores(game_modes: list[str] | None) -> int:
    """Count the rows /latest pages over, for when a request lands past the end.

    Must mirror that query's WHERE clause exactly, or the fallback disagrees
    with the COUNT(*) OVER () the same endpoint returns on a non-empty page.
    It previously counted the whole table: every period bucket, and every mode
    regardless of the filter."""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            if game_modes:
                await cur.execute(
                    """
                    SELECT COUNT(*) FROM scores
                    WHERE game_mode = ANY(%s) AND period = 'alltime'
                    """,
                    (game_modes,),
                )
            else:
                await cur.execute(
                    "SELECT COUNT(*) FROM scores WHERE period = 'alltime'"
                )
            return (await cur.fetchone())[0]

async def _fetch_score_with_rank(user_id: int, game_mode: str, period: str = "alltime") -> ScoreResponse | None:
    #
    """Fetch a single player's score with rank and percentile computed server-side.

    period is assumed to be a valid PERIODS value; callers responsible for validation"""
    period_start = get_period_start(period)

    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT sort_order FROM game_modes WHERE name = %s", (game_mode,)
            )
            mode_row = await cur.fetchone()
            if mode_row is None:
                return None
            order = "ASC" if mode_row[0] == "ASC" else "DESC"

            await cur.execute(
                f"""
                WITH ranked AS (
                    SELECT
                        s.id, u.username, s.score, s.game_mode, s.period, s.submitted_at,
                        s.user_id, s.validation_tier,
                        RANK()  OVER (ORDER BY score {order}, s.submitted_at ASC, s.id ASC) AS rank,
                        COUNT(*) OVER ()                                                AS total_count
                    FROM scores s
                    JOIN users u ON u.id = s.user_id
                    WHERE game_mode    = %s
                      AND period       = %s
                      AND period_start = %s
                )
                SELECT id, username, score, game_mode, period, submitted_at, rank, total_count,
                       validation_tier
                FROM ranked
                WHERE user_id = %s
                LIMIT 1
                """,
                (game_mode, period, period_start, user_id),
            )
            row = await cur.fetchone()

    if row is None:
        return None

    total = row[7]
    return ScoreResponse(
        id=row[0], player=row[1], score=row[2],
        game_mode=row[3], period=row[4],
        submitted_at=row[5].astimezone(UTC).isoformat(),
        rank=row[6],
        percentile=round((1 - (row[6] - 1) / total) * 100, 2) if total > 1 else 100.0,
        validated=row[8] > 0,
        validation_tier=row[8],
    )

async def _apply_score_write(
    cur,
    *,
    user_id: int,
    game_mode: str,
    score: int,
    order: str,
    scoring_strategy: str,
    now: datetime,
    run_id: int | None = None,
    validation_tier: int = 0,
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
            await cur.execute(
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
            await cur.execute(
                """
                INSERT INTO scores
                    (score, game_mode, period, period_start, submitted_at, user_id,
                     run_id, validation_tier)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, game_mode, period, period_start)
                DO UPDATE SET
                    score           = scores.score + EXCLUDED.score,
                    submitted_at    = NOW(),
                    run_id          = EXCLUDED.run_id,
                    validation_tier = EXCLUDED.validation_tier
                """,
                (score, game_mode, period, period_start, now, user_id, run_id, validation_tier),
            )
        return

    # best (default): improvement-gated upsert.
    for period in PERIODS:
        period_start = get_period_start(period, at=now)
        # order is DB-sourced, CHECK-constrained to 'ASC'|'DESC'.
        await cur.execute(
            f"""
            INSERT INTO scores
                (score, game_mode, period, period_start, submitted_at, user_id,
                 run_id, validation_tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, game_mode, period, period_start)
            DO UPDATE SET
                score           = EXCLUDED.score,
                submitted_at    = NOW(),
                run_id          = EXCLUDED.run_id,
                validation_tier = EXCLUDED.validation_tier
            WHERE { _is_improvement_predicate(order) }
            """,
            (score, game_mode, period, period_start, now, user_id, run_id, validation_tier),
        )


def _is_improvement_predicate(order: str) -> str:
    # Returns a SQL fragment: true when EXCLUDED.score is better than stored score
    # ASC = lower score is better (ie race time)
    # DESC = higher score is better (ie points).
    # Update scores when new score "beats" old score
    # (new < stored for ASC, new > stored for DESC)
    if order == "ASC":
        return "EXCLUDED.score < scores.score"
    return "EXCLUDED.score > scores.score"
