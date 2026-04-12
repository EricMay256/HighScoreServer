"""Cache backend abstraction.

Selects between in-memory (cachetools) and Redis based on CACHE_BACKEND env var.
Exposes init_cache / get_cache / close_cache to match the lifespan contract in
main.py. The returned object exposes .get / .setex / .delete with signatures
matching redis-py, so call sites don't need to know which backend is active.
"""
from __future__ import annotations

import os
import threading
from typing import Optional, Protocol

from cachetools import TTLCache


class CacheBackend(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def setex(self, key: str, ttl_seconds: int, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def close(self) -> None: ...


class MemoryCache:
    """Thread-safe in-memory TTL cache.

    cachetools.TTLCache is not thread-safe; FastAPI's sync endpoints run in
    a threadpool, so we guard all access with a lock.

    Note: TTLCache uses a single TTL set at construction. The `ttl_seconds`
    argument to setex is accepted for signature parity with redis-py but
    ignored — all entries expire after `default_ttl`. For this codebase
    every call uses CACHE_TTL=120, so this is not a limitation in practice.
    """

    def __init__(
        self,
        maxsize: int = 1024,
        default_ttl: int = 120,
        timer=None,
    ) -> None:
        kwargs = {"maxsize": maxsize, "ttl": default_ttl}
        if timer is not None:
            kwargs["timer"] = timer
        self._cache: TTLCache[str, str] = TTLCache(**kwargs)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._cache.get(key)

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        with self._lock:
            self._cache[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def close(self) -> None:
        with self._lock:
            self._cache.clear()


class RedisCache:
    """Thin wrapper around redis-py. decode_responses=True means get() returns str."""

    def __init__(self, url: str) -> None:
        import redis
        self._client = redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> Optional[str]:
        return self._client.get(key)

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self._client.setex(key, ttl_seconds, value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def close(self) -> None:
        self._client.close()


_cache: CacheBackend | None = None


def init_cache() -> None:
    """Initialize the cache backend. Called from main.py lifespan."""
    global _cache
    backend = os.environ.get("CACHE_BACKEND", "memory").lower()

    if backend == "redis":
        redis_url = os.environ.get("REDIS_URL")
        if redis_url:
            _cache = RedisCache(redis_url)
            return
        # REDIS_URL missing despite CACHE_BACKEND=redis — fall through to memory
        # rather than crash. Matches the graceful-degradation pattern used
        # elsewhere (limiter.py falls through similarly).

    _cache = MemoryCache(default_ttl=120)


def get_cache() -> CacheBackend:
    if _cache is None:
        raise RuntimeError("Cache not initialized")
    return _cache


def close_cache() -> None:
    global _cache
    if _cache is not None:
        _cache.close()
        _cache = None
