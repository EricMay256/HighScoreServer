from fastapi import Header, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from app.auth import decode_access_token

# existing
async def require_api_key(x_api_key: str = Header(...)) -> None:
    ...

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
        )
    return payload