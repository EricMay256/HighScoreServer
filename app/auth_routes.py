import logging
from fastapi import APIRouter, HTTPException, status
from jose import JWTError
from pydantic import BaseModel, EmailStr, Field

from app.auth import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from app.db import get_conn, release_conn


router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)


# ── Request / response models ──────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str  = Field(..., min_length=3, max_length=64)
    email:    str  = Field(..., max_length=256)
    password: str  = Field(..., min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest) -> TokenResponse:
    password_hash = hash_password(body.password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (body.username, body.email, password_hash),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        # Unique constraint violations surface here — username or email taken.
        # pg error code 23505 is unique_violation; check rather than string-match.
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email already registered",
            )
        logger.error("Registration DB error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    user_id = row[0]
    return TokenResponse(
        access_token=create_access_token(user_id, body.username),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE username = %s",
                (body.username,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error("Login DB error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    # Deliberate: same error for unknown user and wrong password.
    # Distinguishing them leaks whether a username exists.
    if row is None or not verify_password(body.password, row[1]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    user_id = row[0]
    return TokenResponse(
        access_token=create_access_token(user_id, body.username),
        refresh_token=create_refresh_token(user_id),
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
            cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    except Exception as e:
        logger.error("Refresh DB error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        release_conn(conn)

    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user_id, row[0]),
        refresh_token=new_refresh,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest) -> None:
    """
    Revokes the refresh token. The access token remains valid until expiry.

    # DENYLIST HOOK: to invalidate the access token immediately on logout,
    # accept it in the request body or Authorization header, decode it,
    # extract the jti, and write it to Redis here with TTL = remaining expiry.
    # That's the only change needed to get immediate access token revocation.
    """
    revoke_refresh_token(body.refresh_token)