import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta

from jose import JWTError, jwt
import bcrypt

from app.db import get_conn, release_conn


# ── Password hashing ───────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

# ── Guest username generation ──────────────────────────────────────────────

def generate_guest_username() -> str:
    """
    Generates a random guest display name.
    Uniqueness is enforced at the DB level — callers should retry on conflict.
    """
    return f"guest_{secrets.token_hex(4)}"

# ── JWT ────────────────────────────────────────────────────────────────────

ACCESS_TOKEN_EXPIRE_MINUTES = 60


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable not set")
    return secret


def create_access_token(user_id: int, username: str, is_guest: bool) -> str:
    """
    Issues a signed JWT access token.

    Payload carries: sub (user_id), username, exp.

    # DENYLIST HOOK: add a jti claim here when implementing revocation.
    # jti = str(uuid.uuid4())
    # Then write jti → Redis with TTL = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    # on logout / password change.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_guest": is_guest,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": now,
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """
    Verifies signature and expiry. Raises JWTError on any failure.

    # DENYLIST HOOK: after successful decode, extract payload["jti"] and
    # check Redis: if the key exists, raise JWTError("token revoked").
    # This is the only place revocation needs to be checked.
    """
    return jwt.decode(token, _secret(), algorithms=["HS256"])


# ── Refresh tokens ─────────────────────────────────────────────────────────

REFRESH_TOKEN_EXPIRE_DAYS = 7


def _hash_token(raw: str) -> str:
    """SHA-256 hash of the raw token for safe DB storage."""
    return hashlib.sha256(raw.encode()).hexdigest()


def create_refresh_token(user_id: int) -> str:
    """
    Generates a cryptographically random opaque token, persists its hash
    to the DB, and returns the raw token to be sent to the client once.
    """
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_hash, expires_at),
            )
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    return raw


def rotate_refresh_token(raw: str) -> tuple[str, int]:
    """
    Validates an incoming refresh token, deletes it (one-time use),
    and returns a fresh raw token + the associated user_id.

    Raises ValueError if the token is invalid or expired.

    Rotation means a stolen refresh token can only be used once before
    the legitimate client's next refresh invalidates it.
    """
    token_hash = _hash_token(raw)
    now = datetime.now(timezone.utc)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM refresh_tokens
                WHERE token_hash = %s AND expires_at > %s
                RETURNING user_id
                """,
                (token_hash, now),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    if row is None:
        raise ValueError("Invalid or expired refresh token")

    user_id = row[0]
    new_raw = create_refresh_token(user_id)
    return new_raw, user_id


def revoke_refresh_token(raw: str) -> None:
    """Deletes a specific refresh token. Called on logout."""
    token_hash = _hash_token(raw)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM refresh_tokens WHERE token_hash = %s",
                (token_hash,),
            )
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        release_conn(conn)