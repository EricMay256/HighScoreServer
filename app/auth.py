import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta

from jose import jwt
import bcrypt

from app.db import get_pool


# ── Password hashing ───────────────────────────────────────────────────────
#
# bcrypt is CPU-bound by design (the cost factor is the security property) and
# has no async variant. Inside an async handler a direct call would block the
# event loop for the full hash duration, stalling every concurrent request on
# the worker. asyncio.to_thread offloads it to the default threadpool — and
# bcrypt's C implementation releases the GIL while hashing, so it runs on
# another core rather than just yielding. This preserves the behavior of the
# prior sync-handler-in-threadpool model.

async def hash_password(plain: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, plain)

async def verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, plain, hashed)

def _hash_password_sync(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def _verify_password_sync(plain: str, hashed: str) -> bool:
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


async def create_refresh_token(user_id: int) -> str:
    """
    Generates a cryptographically random opaque token, persists its hash
    to the DB, and returns the raw token to be sent to the client once.
    This currently runs in its own transaction - when the scale demands
    stronger atomicity guarantees, add optional conn parameter to share
    caller's transaction with user creation / update.
    """
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_hash, expires_at),
            )
        # connection context manager commits on clean exit, rolls back on error

    return raw


async def rotate_refresh_token(raw: str) -> tuple[str, int]:
    """
    Validates an incoming refresh token, deletes it (one-time use),
    inserts its replacement, and returns the new raw token + user_id.

    The delete and insert run in a single transaction on one connection,
    so a failure after the delete cannot silently log the user out.
    Rotation means a stolen refresh token can only be used once before
    the legitimate client's next refresh invalidates it.

    Raises ValueError if the token is invalid or expired.
    """
    token_hash  = _hash_token(raw)
    now         = datetime.now(timezone.utc)
    new_raw     = secrets.token_urlsafe(32)
    new_hash    = _hash_token(new_raw)
    new_expires = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM refresh_tokens
                WHERE token_hash = %s AND expires_at > %s
                RETURNING user_id
                """,
                (token_hash, now),
            )
            row = await cur.fetchone()
            if row is None:
                # Raising inside the CM rolls back the (delete-only) transaction.
                raise ValueError("Invalid or expired refresh token")

            user_id = row[0]
            await cur.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, new_hash, new_expires),
            )
        # DELETE + INSERT commit together when the connection CM exits cleanly

    return new_raw, user_id


async def revoke_refresh_token(raw: str) -> None:
    """Deletes a specific refresh token. Called on logout."""
    token_hash = _hash_token(raw)
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM refresh_tokens WHERE token_hash = %s",
                (token_hash,),
            )