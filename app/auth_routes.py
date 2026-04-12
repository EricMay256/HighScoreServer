import logging
from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from pydantic import BaseModel, EmailStr, Field
from app.limiter import limiter
from starlette.requests import Request

from app.auth import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    generate_guest_username,
    hash_password,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from app.db import get_conn, release_conn
from app.dependencies import require_user


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

class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/guest", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def guest_login(request: Request) -> TokenResponse:
    """
    Creates a guest account with a generated username.
    Retries on the rare username collision (token_hex(4) = 4 billion combinations).
    Called once on first Unity client launch; token stored in PlayerPrefs.
    """
    for _ in range(5):
        username = generate_guest_username()
        conn     = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, is_guest)
                    VALUES (%s, TRUE)
                    ON CONFLICT (username) DO NOTHING
                    RETURNING id, is_guest
                    """,
                    (username,),
                )
                row = cur.fetchone()
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error("Guest registration error: %s", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
        finally:
            release_conn(conn)

        if row:
            return TokenResponse(
                access_token=create_access_token(row[0], username, is_guest=True),
                # Note: user INSERT and refresh token INSERT are separate transactions.
                # A crash between them leaves an orphaned user row with no token.
                # The client will receive an error and can retry. See auth.py for discussion.
                refresh_token=create_refresh_token(row[0]),
            )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to generate unique guest username, please retry",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest) -> TokenResponse:
    password_hash = hash_password(body.password)
    conn          = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, is_guest)
                VALUES (%s, %s, %s, FALSE)
                RETURNING id
                """,
                (body.username, body.email, password_hash),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email already registered",
            )
        logger.error("Registration error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    return TokenResponse(
        access_token=create_access_token(row[0], body.username, is_guest=False),
        # Note: user INSERT and refresh token INSERT are separate transactions.
        # A crash between them leaves an orphaned user row with no token.
        # The client will receive an error and can retry. See auth.py for discussion.
        refresh_token=create_refresh_token(row[0]),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest) -> TokenResponse:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash, is_guest FROM users WHERE username = %s",
                (body.username,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error("Login error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    if row is None or not row[1] or not verify_password(body.password, row[1]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    return TokenResponse(
        access_token=create_access_token(row[0], body.username, is_guest=row[2]),
        refresh_token=create_refresh_token(row[0]),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest) -> TokenResponse:
    try:
        new_refresh, user_id = rotate_refresh_token(body.refresh_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, is_guest FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error("Refresh error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=row[1]),
        refresh_token=new_refresh,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest) -> None:
    """
    # DENYLIST HOOK: to immediately invalidate the access token on logout,
    # accept it in the request body, decode it, extract jti, write to Redis
    # with TTL = remaining expiry seconds.
    """
    revoke_refresh_token(body.refresh_token)


@router.post("/rename", status_code=status.HTTP_204_NO_CONTENT)
def rename(
    body:    RenameRequest,
    payload: dict = Depends(require_user),
) -> None:
    user_id      = int(payload["sub"])
    new_username = body.username

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET username = %s WHERE id = %s",
                (new_username, user_id),
            )
            conn.commit()
    except Exception as e:
        conn.rollback()
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username is already taken",
            )
        logger.error("Rename error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)


@router.post("/claim", response_model=TokenResponse)
def claim(
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

    password_hash = hash_password(body.password)
    conn          = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
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
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )
        logger.error("Claim error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user_id, row[0], is_guest=False),
        refresh_token=create_refresh_token(user_id),
    )