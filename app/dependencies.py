import os
from fastapi import Header, HTTPException, status


async def require_api_key(x_api_key: str = Header(...)) -> None:
    expected = os.environ.get("API_KEY")
    if not expected:
        raise RuntimeError("API_KEY environment variable not set")
    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )