import re
import secrets
from dataclasses import dataclass

from psycopg import errors

from app.db import get_pool


NATIVE_AUTH_PROVIDER = "ubear"
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class AuthIdentityError(ValueError):
    """Base error for auth identity operations."""


class AuthIdentityConflict(AuthIdentityError):
    """Raised when an identity is already linked to a different user."""


class AuthIdentityUserNotFound(AuthIdentityError):
    """Raised when a requested user does not exist."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    username: str
    is_guest: bool


def validate_provider(provider: str) -> str:
    """Normalize and validate an auth provider key used in SQL uniqueness."""
    normalized = provider.strip().lower()
    if not _PROVIDER_RE.fullmatch(normalized):
        raise AuthIdentityError("Invalid auth provider")
    return normalized


def generate_identity_username(provider: str) -> str:
    """Generate a unique-candidate username for provider-first account creation."""
    safe_provider = validate_provider(provider)
    return f"{safe_provider}_{secrets.token_hex(4)}"


async def find_user_by_auth_identity(
    provider: str,
    provider_user_id: str,
) -> AuthenticatedUser | None:
    """Resolve a provider identity to the canonical HSS user, if it exists."""
    normalized_provider = validate_provider(provider)
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT u.id, u.username, u.is_guest
                FROM auth_identities ai
                JOIN users u ON u.id = ai.user_id
                WHERE ai.provider = %s
                  AND ai.provider_user_id = %s
                """,
                (normalized_provider, provider_user_id),
            )
            row = await cur.fetchone()

    if row is None:
        return None
    return AuthenticatedUser(id=row[0], username=row[1], is_guest=row[2])


async def resolve_auth_identity_login(
    provider: str,
    provider_user_id: str,
) -> AuthenticatedUser:
    """
    Resolve a verified provider identity, creating a durable account on first login.

    Callers must validate the provider credential before passing its stable subject
    id here. For Steam, that means verifying the session ticket server-side and
    passing the resulting SteamID64, never a client-supplied id.
    """
    existing = await find_user_by_auth_identity(provider, provider_user_id)
    if existing is not None:
        return existing

    normalized_provider = validate_provider(provider)
    for _ in range(5):
        username = generate_identity_username(normalized_provider)
        try:
            async with get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO users (username, is_guest)
                        VALUES (%s, FALSE)
                        ON CONFLICT (username) DO NOTHING
                        RETURNING id, username, is_guest
                        """,
                        (username,),
                    )
                    user_row = await cur.fetchone()
                    if user_row is None:
                        continue

                    await cur.execute(
                        """
                        INSERT INTO auth_identities (
                            user_id, provider, provider_user_id
                        )
                        VALUES (%s, %s, %s)
                        """,
                        (user_row[0], normalized_provider, provider_user_id),
                    )
                    return AuthenticatedUser(
                        id=user_row[0],
                        username=user_row[1],
                        is_guest=user_row[2],
                    )
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name != "auth_identities_provider_user_id_key":
                raise
            raced = await find_user_by_auth_identity(normalized_provider, provider_user_id)
            if raced is not None:
                return raced

    raise AuthIdentityError("Failed to create a unique username for identity login")


async def attach_auth_identity_to_user(
    user_id: int,
    provider: str,
    provider_user_id: str,
) -> AuthenticatedUser:
    """
    Link a verified provider identity to an existing account.

    Guests are upgraded in place because a linked external identity is durable
    account proof. Re-linking the same identity to the same user is idempotent;
    linking an identity owned by another user raises AuthIdentityConflict.
    """
    normalized_provider = validate_provider(provider)
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE users
                SET is_guest = FALSE
                WHERE id = %s
                RETURNING id, username, is_guest
                """,
                (user_id,),
            )
            user_row = await cur.fetchone()
            if user_row is None:
                raise AuthIdentityUserNotFound("User not found")

            await cur.execute(
                """
                INSERT INTO auth_identities (user_id, provider, provider_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (provider, provider_user_id) DO NOTHING
                RETURNING user_id
                """,
                (user_id, normalized_provider, provider_user_id),
            )
            identity_row = await cur.fetchone()
            if identity_row is None:
                await cur.execute(
                    """
                    SELECT user_id
                    FROM auth_identities
                    WHERE provider = %s
                      AND provider_user_id = %s
                    """,
                    (normalized_provider, provider_user_id),
                )
                owner_row = await cur.fetchone()
                if owner_row is None or owner_row[0] != user_id:
                    raise AuthIdentityConflict("Identity is already linked")

    return AuthenticatedUser(id=user_row[0], username=user_row[1], is_guest=user_row[2])
