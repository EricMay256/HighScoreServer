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
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

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


@router.post("/rename", status_code=status.HTTP_204_NO_CONTENT)
async def rename(
    body:    RenameRequest,
    payload: dict = Depends(require_user),
) -> None:
    user_id      = int(payload["sub"])
    new_username = body.username

    try:
        async with get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET username = %s WHERE id = %s",
                    (new_username, user_id),
                )
    except Exception as e:
        if getattr(e, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username is already taken",
            ) from e
        logger.error("Rename error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e


@router.post("/claim", response_model=TokenResponse)
async def claim(
    body:    ClaimRequest,
    payload: dict = Depends(require_user),
) -> TokenResponse:
    """
    Upgrades a guest account to a claimed account by attaching
    email and password. Issues fresh tokens reflecting is_guest=False.
    """
    user_id = int(payload["sub"])

    if not payload.get("is_guest"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is already claimed",
        )

    password_hash = await hash_password(body.password)
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
    except Exception as e:
        if getattr(e, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            ) from e
        logger.error("Claim error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=False),
        refresh_token=await create_refresh_token(user_id),
    )
