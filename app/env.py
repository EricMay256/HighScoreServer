import os
from functools import lru_cache

from dotenv import find_dotenv, load_dotenv

REQUIRED_ENV_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "API_KEY",
)


@lru_cache(maxsize=1)
def load_environment() -> None:
    """Load environment variables from .env once per process."""
    env_file = find_dotenv(usecwd=True)
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        # Keep behavior safe in deployed environments where .env may not exist.
        load_dotenv(override=False)


def validate_environment(required_vars: tuple[str, ...] = REQUIRED_ENV_VARS) -> None:
    """Raise a readable startup error if required environment vars are missing."""
    missing = [name for name in required_vars if not os.environ.get(name)]
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            "Missing required environment variables: "
            f"{missing_list}. Set them in the process environment or .env."
        )
