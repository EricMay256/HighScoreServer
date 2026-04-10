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
    Falls back to in-process memory if Redis is unreachable.

    The tradeoff: memory storage doesn't share state across gunicorn workers
    or dynos, so limits are per-process in degraded mode. Acceptable — the
    alternative is taking the API down when Redis has a blip.
    """
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(redis_url)
            client.ping()
            logger.info("Rate limiter using Redis storage: %s", redis_url)
            return Limiter(key_func=get_real_ip, storage_uri=redis_url)
        except Exception as e:
            logger.warning(
                "Redis unavailable for rate limiter, falling back to memory: %s", e
            )
    return Limiter(key_func=get_real_ip, storage_uri="memory://")


limiter = _make_limiter()