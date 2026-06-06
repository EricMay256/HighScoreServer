"""Cache backend abstraction.

Selects between in-memory (cachetools) and Redis based on CACHE_BACKEND env var.
Exposes init_cache / get_cache / close_cache to match the lifespan contract in
main.py. The returned object exposes async .get / .setex / .delete with
signatures matching redis-py's async client, so call sites await the same
interface regardless of which backend is active.

The interface is async because the Redis backend performs network I/O on the
event loop; the in-memory backend's methods are async only for interface parity
(their bodies are trivial CPU work).
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

from cachetools import TTLCache


class CacheBackend(Protocol):
    async def get(self, key: str) -> Optional[str]: ...
    async def setex(self, key: str, ttl_seconds: int, value: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def close(self) -> None: ...


class MemoryCache:
    """In-memory TTL cache.

    Under async handlers all access happens on the single event-loop thread,
    so no lock is needed (unlike the prior sync-handler-in-threadpool model).
    The methods are async purely for interface parity with RedisCache.

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

    async def get(self, key: str) -> Optional[str]:
        return self._cache.get(key)

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self._cache[key] = value

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete all keys starting with `prefix`. Returns count of deleted entries.

        cachetools.TTLCache exposes .keys() but mutating during iteration is
        unsafe; collect matches first, then pop. There is no concurrency to
        guard against — the event loop runs this coroutine to completion with
        no await point between collecting and popping.
        """
        matches = [k for k in self._cache.keys() if k.startswith(prefix)]
        for k in matches:
            self._cache.pop(k, None)
        return len(matches)

    async def close(self) -> None:
        self._cache.clear()


class RedisCache:
    """Thin wrapper around redis-py's async client.

    decode_responses=True means get() returns str. Uses redis.asyncio so the
    network round-trips await rather than blocking the event loop.
    """

    def __init__(self, url: str) -> None:
        from redis.asyncio import from_url
        self._client = from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[str]:
        return await self._client.get(key)

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        await self._client.setex(key, ttl_seconds, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete all keys starting with `prefix`. Returns count of deleted keys.
        Uses SCAN rather than KEYS to avoid blocking the Redis server on large
        keyspaces. SCAN returns keys in batches; we collect them and delete in
        chunks. count=100 is a hint to the server about batch size, not a cap.
        """
        deleted = 0
        pattern = f"{prefix}*"
        # scan_iter handles cursor management internally
        keys_to_delete: list[str] = []
        async for key in self._client.scan_iter(match=pattern, count=100):
            keys_to_delete.append(key)
            # Delete in batches of 500 to bound memory and round-trip size
            if len(keys_to_delete) >= 500:
                deleted += await self._client.delete(*keys_to_delete)
                keys_to_delete = []
        if keys_to_delete:
            deleted += await self._client.delete(*keys_to_delete)
        return deleted

    async def close(self) -> None:
        await self._client.aclose()


_cache: CacheBackend | None = None


def init_cache() -> None:
    """Initialize the cache backend. Called from main.py lifespan.

    Construction is synchronous; the async surface is on the get/setex/etc.
    methods. redis.asyncio's from_url is lazy and does not connect here.
    """
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


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
