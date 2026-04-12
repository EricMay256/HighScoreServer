# 0007. In-process cache over Redis at single-dyno scale

Date: 2026-04-12

## Status

Accepted

## Context

The application has two pieces of state that benefit from caching: leaderboard query results (which are read-heavy and expensive to compute, involving window functions over the `scores` table), and per-IP rate limit counters (consumed by slowapi on every rate-limited request). Both have natural homes in either an in-process store or Redis.

The cache layer in `app/cache.py` was built backend-pluggable from the start: it exposes a small `get` / `set` / `delete` interface with two implementations, an in-process `cachetools.TTLCache` and a Redis client. The slowapi rate limiter has the same shape — it accepts either a memory backend or a Redis URI. Selection is driven by the `CACHE_BACKEND` environment variable, set once at startup.

The deployment is a single Heroku web dyno running a single worker process. Earlier in the project's life the Heroku Redis add-on was provisioned and `CACHE_BACKEND=redis` was set, on the assumption that "production should use Redis." Reviewing this in the context of the actual deployment topology made it clear the assumption was unexamined: every request hits the same process, so every request sees the same cache state regardless of which backend is selected. The Redis add-on was adding a network hop, an external dependency that can fail independently, and a monthly bill, in exchange for behavior the in-process cache was already providing.

## Decision

Use the in-process backends (`cachetools.TTLCache` for caching, slowapi's memory storage for rate limiting) as the deployed configuration. Set `CACHE_BACKEND=memory`. Remove the Heroku Redis add-on.

Retain the Redis code path in `app/cache.py` and the slowapi Redis configuration. Re-enabling Redis is a config change (`heroku addons:create heroku-redis:mini` plus `heroku config:set CACHE_BACKEND=redis`), not a code change.

## Consequences

**Positive.** The deployed system has one fewer external dependency and one fewer source of latency on every cached request. The application can no longer fail because Redis is unreachable. Operating cost is reduced. The cache behavior in production matches the cache behavior in local development and tests, which previously diverged based on whether Memurai was running.

**Negative.** The decision rests entirely on the single-dyno, single-worker assumption. The moment that assumption stops being true — a second dyno, a second uvicorn worker, a background job process that needs to share cache state — the in-process backend silently becomes wrong in two distinct ways:

1. **Cache coherence.** Each process maintains its own cache. A score submission served by process A invalidates process A's cache key, but process B will continue serving stale leaderboard data until its own copy expires by TTL. This is a correctness regression in the cache invalidation contract that score submission relies on.
2. **Rate limit enforcement.** slowapi's memory backend counts requests per-process. With N workers, an attacker can effectively make N times the documented rate limit by getting load-balanced across them. This is the more dangerous of the two failure modes because it is a security regression, not a freshness regression, and it produces no visible symptom until someone tries to abuse it.

The mitigation is to treat any move beyond single-process deployment as an explicit trigger to flip `CACHE_BACKEND` to `redis` before the new process count goes live. The code path being retained is what makes that a same-day fix instead of a refactor.