import os
import redis


_redis_client: redis.Redis | None = None


def init_cache() -> None:
    global _redis_client
    _redis_client = redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
    )


def get_cache() -> redis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized")
    return _redis_client


def close_cache() -> None:
    if _redis_client is not None:
        _redis_client.close()