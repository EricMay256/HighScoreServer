import logging
import os

from slowapi import Limiter
from starlette.requests import Request

logger = logging.getLogger(__name__)


def get_real_ip(request: Request) -> str:
    """
    Extracts the real client IP from X-Forwarded-For when behind Heroku's
    load balancer. Falls back to direct connection address for local dev.

    X-Forwarded-For can contain a comma-separated chain of IPs if the request
    passed through multiple proxies: "client, proxy1, proxy2". The leftmost
    entry is the original client — we always want index 0.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host


def _make_limiter() -> Limiter:
    """
    Attempts to connect to Redis for distributed rate limiting.
    Uses Redis only when CACHE_BACKEND=redis AND REDIS_URL is set, so the
    rate limiter's storage backend tracks the cache by design rather than
    by accident of env var layout (see ADR 0007). Falls back to in-process
    memory if Redis is configured but unreachable — a Redis blip degrades
    rate limiting rather than taking the API down.

    The tradeoff: memory storage doesn't share state across gunicorn workers
    or dynos, so limits are per-process in degraded mode. Acceptable — the
    alternative is taking the API down when Redis has a blip.
    """
    enabled = os.environ.get("RATE_LIMITER_ENABLED", "true").lower() != "false"

    cache_backend = os.environ.get("CACHE_BACKEND", "memory").lower()
    redis_url = os.environ.get("REDIS_URL")
    if cache_backend == "redis" and redis_url:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(redis_url)
            client.ping()
            logger.info("Rate limiter using Redis storage")
            return Limiter(
                key_func=get_real_ip,
                storage_uri=redis_url,
                enabled=enabled,
            )
        except Exception as e:
            logger.warning(
                "Redis unavailable for rate limiter, falling back to memory: %s", e
            )
    elif cache_backend == "redis" and not redis_url:
        logger.warning(
            "CACHE_BACKEND=redis but REDIS_URL is unset; rate limiter falling back to memory"
        )

    return Limiter(
        key_func=get_real_ip,
        storage_uri="memory://",
        enabled=enabled,
    )


limiter = _make_limiter()