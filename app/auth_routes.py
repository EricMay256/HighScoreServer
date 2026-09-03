import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from starlette.requests import Request

from app.auth import (
    create_access_token,
    create_refresh_token,
    generate_guest_username,
    hash_password,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from app.auth_identities import (
    NATIVE_AUTH_PROVIDER,
    AuthenticatedUser,
    AuthIdentityConflict,
    AuthIdentityUserNotFound,
    attach_auth_identity_to_user,
    resolve_auth_identity_login,
)
from app.cache import get_cache
from app.db import get_pool
from app.dependencies import require_user
from app.limiter import limiter, rate_limited_responses
from app.steam_auth import (
    STEAM_AUTH_PROVIDER,
    SteamAuthConfigError,
    SteamAuthInvalidTicket,
    SteamAuthUpstreamError,
    verify_steam_auth_ticket,
)


router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)


# ── Request / response models ──────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str      = Field(..., min_length=3, max_length=64)
    email:    EmailStr = Field(..., max_length=256)
    password: str      = Field(..., min_length=8)

class LoginRequest(BaseModel):
    username: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

class RenameRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)

class ClaimRequest(BaseModel):
    email:    EmailStr = Field(..., max_length=256)
    password: str      = Field(..., min_length=8)

class SteamAuthRequest(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=8192)

class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"

class AccessTokenResponse(BaseModel):
    """A replacement access token, with the caller's refresh token untouched.

    For an operation that invalidates what the *access* token says but leaves
    the session itself alone. Minting a refresh token here would be a second
    live credential the caller never asked for and cannot be told to discard,
    because the old one keeps working -- see /rename.
    """
    access_token: str
    token_type:   str = "bearer"


async def token_response_for_user(user: AuthenticatedUser) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.username, is_guest=user.is_guest),
        refresh_token=await create_refresh_token(user.id),
    )


async def steam_id_from_ticket(ticket: str) -> str:
    try:
        return await verify_steam_auth_ticket(ticket)
    except SteamAuthConfigError as e:
        logger.error("Steam auth configuration error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Steam authentication is not configured",
        ) from e
    except SteamAuthInvalidTicket:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Steam auth ticket",
        ) from None
    except SteamAuthUpstreamError as e:
        logger.error("Steam auth upstream error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Steam authentication is temporarily unavailable",
        ) from e


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/guest", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             responses=rate_limited_responses("5 per minute"))
@limiter.limit("5/minute")
async def guest_login(request: Request, response: Response) -> TokenResponse:
    """
    Creates a guest account with a generated username.
    Retries on the rare username collision (token_hex(4) = 4 billion combinations).
    Called once on first Unity client launch; token stored in PlayerPrefs.
    """
    for _ in range(5):
        username = generate_guest_username()
        try:
            async with get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO users (username, is_guest)
                        VALUES (%s, TRUE)
                        ON CONFLICT (username) DO NOTHING
                        RETURNING id, is_guest
                        """,
                        (username,),
                    )
                    row = await cur.fetchone()
        except Exception as e:
            logger.error("Guest registration error: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            ) from e

        if row:
            return TokenResponse(
                access_token=create_access_token(row[0], username, is_guest=True),
                # Note: user INSERT and refresh token INSERT are separate transactions.
                # A crash between them leaves an orphaned user row with no token.
                # The client will receive an error and can retry. See auth.py for discussion.
                refresh_token=await create_refresh_token(row[0]),
            )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to generate unique guest username, please retry",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             responses=rate_limited_responses("10 per minute"))
@limiter.limit("10/minute")
async def register(request: Request, response: Response, body: RegisterRequest) -> TokenResponse:
    password_hash = await hash_password(body.password)
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (username, email, password_hash, is_guest)
                    VALUES (%s, %s, %s, FALSE)
                    RETURNING id
                    """,
                    (body.username, body.email, password_hash),
                )
                row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO auth_identities (user_id, provider, provider_user_id)
                    VALUES (%s, %s, %s)
                    """,
                    (row[0], NATIVE_AUTH_PROVIDER, body.email),
                )
    except Exception as e:
        if getattr(e, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email already registered",
            ) from e
        logger.error("Registration error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    return TokenResponse(
        access_token=create_access_token(row[0], body.username, is_guest=False),
        # Note: user INSERT and refresh token INSERT are separate transactions.
        # A crash between them leaves an orphaned user row with no token.
        # The client will receive an error and can retry. See auth.py for discussion.
        refresh_token=await create_refresh_token(row[0]),
    )


@router.post("/login", response_model=TokenResponse, responses=rate_limited_responses("10 per minute"))
@limiter.limit("10/minute")
async def login(request: Request, response: Response, body: LoginRequest) -> TokenResponse:
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, password_hash, is_guest FROM users WHERE username = %s",
                    (body.username,),
                )
                row = await cur.fetchone()
    except Exception as e:
        logger.error("Login error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if row is None or not row[1] or not await verify_password(body.password, row[1]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    return TokenResponse(
        access_token=create_access_token(row[0], body.username, is_guest=row[2]),
        refresh_token=await create_refresh_token(row[0]),
    )


@router.post(
    "/steam/login",
    response_model=TokenResponse,
    responses=rate_limited_responses("10 per minute"),
)
@limiter.limit("10/minute")
async def steam_login(
    request: Request,
    response: Response,
    body: SteamAuthRequest,
) -> TokenResponse:
    steam_id = await steam_id_from_ticket(body.ticket)
    user = await resolve_auth_identity_login(STEAM_AUTH_PROVIDER, steam_id)
    return await token_response_for_user(user)


@router.post(
    "/steam/link",
    response_model=TokenResponse,
    responses=rate_limited_responses("10 per minute"),
)
@limiter.limit("10/minute")
async def steam_link(
    request: Request,
    response: Response,
    body: SteamAuthRequest,
    payload: dict = Depends(require_user),
) -> TokenResponse:
    steam_id = await steam_id_from_ticket(body.ticket)
    user_id = int(payload["sub"])

    try:
        user = await attach_auth_identity_to_user(user_id, STEAM_AUTH_PROVIDER, steam_id)
    except AuthIdentityConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Steam account is already linked",
        ) from None
    except AuthIdentityUserNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from None

    return await token_response_for_user(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest) -> TokenResponse:
    try:
        new_refresh, user_id = await rotate_refresh_token(body.refresh_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from None

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT username, is_guest FROM users WHERE id = %s",
                    (user_id,),
                )
                row = await cur.fetchone()
    except Exception as e:
        logger.error("Refresh error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=row[1]),
        refresh_token=new_refresh,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest) -> None:
    """
    # DENYLIST HOOK: to immediately invalidate the access token on logout,
    # accept it in the request body, decode it, extract jti, write to Redis
    # with TTL = remaining expiry seconds.
    """
    await revoke_refresh_token(body.refresh_token)


async def _invalidate_leaderboard_caches() -> None:
    """Drop every cached leaderboard read after a username changes.

    Imported rather than duplicated: `CACHE_KEY_PREFIX` is the leaderboard's
    own statement of how it keys its cache, and a second copy here would drift
    the moment either side changed. leaderboard_routes does not import this
    module, so the direction is safe.

    Best effort, like the leaderboard's own invalidation: a cache that cannot
    be cleared is a stale name for two minutes, not a failed rename.
    """

    from app.leaderboard_routes import CACHE_KEY_PREFIX

    try:
        await get_cache().delete_prefix(CACHE_KEY_PREFIX)
    except Exception as e:
        logger.warning("Leaderboard cache invalidation failed after rename: %s", e)


@router.post(
    "/rename",
    response_model=AccessTokenResponse,
    responses=rate_limited_responses("10 per minute"),
)
@limiter.limit("10/minute")
async def rename(
    request:  Request,
    response: Response,
    body:     RenameRequest,
    payload:  dict = Depends(require_user),
) -> AccessTokenResponse:
    """
    Changes the username and returns a replacement access token.

    The access token carries `username` as a claim, so a rename that returned
    nothing left every client showing the old name until the token expired and
    a refresh happened to mint a new one.

    **The access token only.** The refresh token is opaque and carries no
    username, so a rename does not invalidate it and there is nothing to
    replace. Minting one anyway -- which this did until 2026-09-03, by copying
    /claim -- left the previous credential valid for its full lifetime and
    added a row per rename, so a client renaming in a loop grew
    `refresh_tokens` without bound and accumulated live credentials nobody
    could enumerate a reason for. /claim gets away with the same shape because
    it can only succeed once per account; rename has no such limit.

    Rate limited like its sibling auth routes, which it should have been from
    the start: it takes a write lock on a `users` row.

    It was not "the only auth route without a bucket", as this said until
    2026-09-03. `/refresh` and `/logout` are still unlimited. Neither hashes a
    password, so neither is the CPU amplifier `/claim` was, but they are not
    limited and nothing here should be read as saying they are.
    """
    user_id      = int(payload["sub"])
    new_username = body.username

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE users SET username = %s WHERE id = %s
                    RETURNING username, is_guest
                    """,
                    (new_username, user_id),
                )
                row = await cur.fetchone()
    except Exception as e:
        if getattr(e, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username is already taken",
            ) from e
        logger.error("Rename error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Leaderboard rows carry the username, and the cached ones carry the old
    # one. Without this the header updates from the new token immediately while
    # the board underneath keeps showing the previous name for up to the 120s
    # TTL -- the same page disagreeing with itself about who the reader is.
    #
    # The whole namespace, not this user's modes: a rename does not say which
    # boards they appear on, and finding out costs a query to save a cache fill
    # on an operation that happens approximately never.
    await _invalidate_leaderboard_caches()

    return AccessTokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=row[1]),
    )


@router.post(
    "/claim",
    response_model=TokenResponse,
    responses=rate_limited_responses("5 per minute"),
)
@limiter.limit("5/minute")
async def claim(
    request:  Request,
    response: Response,
    body:     ClaimRequest,
    payload:  dict = Depends(require_user),
) -> TokenResponse:
    """
    Upgrades a guest account to a claimed account by attaching
    email and password. Issues fresh tokens reflecting is_guest=False.

    Rate limited, and the account state is read before the password is
    hashed. Both matter here specifically: bcrypt is deliberately expensive
    and runs on a worker thread, so an unlimited route that hashes before
    checking anything is a CPU amplifier for whoever holds a token.
    """
    user_id = int(payload["sub"])

    # The token's own claim, which is up to an hour stale and -- because a
    # successful claim does not revoke the guest token that authorized it --
    # keeps saying `is_guest` long after the account stopped being one.
    if not payload.get("is_guest"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is already claimed",
        )

    # So ask the database before doing the expensive part. Without this, a
    # replayed guest token reached `hash_password` on every attempt for the
    # life of the token, each one occupying an executor thread, and only then
    # learned from the UPDATE that there was nothing to claim.
    #
    # An early exit, not the correctness guard: the UPDATE below keeps its own
    # `AND is_guest = TRUE`, which is what actually settles a race between two
    # concurrent claims. This only avoids paying bcrypt to find out.
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT is_guest FROM users WHERE id = %s", (user_id,)
                )
                current = await cur.fetchone()
    except Exception as e:
        logger.error("Claim precheck error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not current[0]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is already claimed",
        )

    password_hash = await hash_password(body.password)
    existing_user = None
    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE users
                    SET email         = %s,
                        password_hash = %s,
                        is_guest      = FALSE
                    WHERE id = %s
                      AND is_guest = TRUE
                    RETURNING username
                    """,
                    (body.email, password_hash, user_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    await cur.execute(
                        """
                        INSERT INTO auth_identities (user_id, provider, provider_user_id)
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, NATIVE_AUTH_PROVIDER, body.email),
                    )
                else:
                    await cur.execute(
                        "SELECT is_guest FROM users WHERE id = %s",
                        (user_id,),
                    )
                    existing_user = await cur.fetchone()
    except Exception as e:
        if getattr(e, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            ) from e
        logger.error("Claim error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if row is None:
        if existing_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is already claimed",
        )

    return TokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=False),
        refresh_token=await create_refresh_token(user_id),
    )
