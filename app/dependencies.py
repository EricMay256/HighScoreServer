import hmac
import os

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from app.auth import decode_access_token


# existing
async def require_api_key(x_api_key: str = Header(...)) -> None:
    expected = os.environ.get("API_KEY")
    if not expected:
        raise RuntimeError("API_KEY environment variable not set")
    # Constant-time, matching the vault's auth.secret_matches. A timing attack
    # across Heroku's router is impractical; comparing secrets with == is the
    # kind of thing that stops being harmless once the code is copied.
    if not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

# new
_bearer = HTTPBearer()

async def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Validates the Bearer JWT and returns the decoded payload.
    Inject as a dependency on any route that needs an authenticated user.

    Raises 401 on missing, malformed, or expired tokens.

    # DENYLIST HOOK: decode_access_token already has the hook comment inside it.
    # No changes needed here — revocation is handled at the decode layer.
    """
    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return payload
