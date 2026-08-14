"""Token-bucket behaviour, driven by an injected clock rather than sleeping.

The pre-auth IP guard is exercised at the bottom, through a synthetic router
shaped like the vault's. It needs no database precisely because the property
under test is that a refused request never reaches one.
"""

import asyncio

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.vault.rate_limit import (
    LIMITS,
    Limit,
    TokenBucketLimiter,
    build_preauth_dependency,
    client_ip,
    enforce_preauth_ip_limit,
)


def test_burst_is_the_capacity_and_the_next_request_is_refused() -> None:
    limiter = TokenBucketLimiter()
    limit = LIMITS["search"]

    async def exercise() -> None:
        for _ in range(limit.burst):
            assert await limiter.check("agent", "search", now=0.0) is None

        retry_after = await limiter.check("agent", "search", now=0.0)
        assert retry_after is not None
        assert retry_after >= 1.0

    asyncio.run(exercise())


def test_tokens_refill_at_the_sustained_rate() -> None:
    limiter = TokenBucketLimiter()
    limit = LIMITS["search"]

    async def exercise() -> None:
        for _ in range(limit.burst):
            await limiter.check("agent", "search", now=0.0)
        assert await limiter.check("agent", "search", now=0.0) is not None

        # One token takes 1/refill_per_second seconds; 30/min is one every 2s.
        one_token = 1.0 / limit.refill_per_second
        assert await limiter.check("agent", "search", now=one_token) is None
        # …and only one.
        assert await limiter.check("agent", "search", now=one_token) is not None

    asyncio.run(exercise())


def test_an_idle_bucket_does_not_accumulate_beyond_the_burst() -> None:
    """A quiet week must not buy a week's worth of requests at once."""

    limiter = TokenBucketLimiter()
    limit = LIMITS["search"]

    async def exercise() -> None:
        await limiter.check("agent", "search", now=0.0)
        allowed = 0
        for _ in range(limit.burst * 5):
            if await limiter.check("agent", "search", now=86_400.0) is None:
                allowed += 1
        assert allowed == limit.burst

    asyncio.run(exercise())


def test_principals_and_operations_have_separate_buckets() -> None:
    """One noisy agent must not spend another's quota, nor search spend fetch."""

    limiter = TokenBucketLimiter()

    async def exercise() -> None:
        for _ in range(LIMITS["search"].burst):
            await limiter.check("noisy", "search", now=0.0)
        assert await limiter.check("noisy", "search", now=0.0) is not None

        assert await limiter.check("quiet", "search", now=0.0) is None
        assert await limiter.check("noisy", "get_note", now=0.0) is None

    asyncio.run(exercise())


def test_retry_after_is_never_below_one_second() -> None:
    """A sub-second Retry-After truncates to 0 and invites an instant retry."""

    limiter = TokenBucketLimiter()

    async def exercise() -> None:
        # 120/min refills a token every 0.5s, so the raw deficit is sub-second.
        for _ in range(LIMITS["get_note"].burst):
            await limiter.check("agent", "get_note", now=0.0)
        retry_after = await limiter.check("agent", "get_note", now=0.0)
        assert retry_after is not None
        assert retry_after >= 1.0

    asyncio.run(exercise())


def test_pruning_never_refunds_a_partly_drained_bucket() -> None:
    """The subtle failure this replaced.

    Dropping a bucket merely because it *looks* full after a refill would hand
    the next request a fresh full bucket and forget everything already charged.
    A bucket may only be dropped when elapsed time proves it would have
    refilled anyway.
    """

    limiter = TokenBucketLimiter()
    limit = LIMITS["search"]

    async def exercise() -> None:
        for _ in range(limit.burst):
            assert await limiter.check("agent", "search", now=0.0) is None
        # Prune aggressively at a moment that does NOT prove a refill.
        limiter._prune(0.0)
        # The drained bucket must survive, so this is still refused.
        assert await limiter.check("agent", "search", now=0.0) is not None

    asyncio.run(exercise())


def test_pruning_drops_buckets_that_have_provably_refilled() -> None:
    limiter = TokenBucketLimiter()
    limit = LIMITS["search"]

    async def exercise() -> None:
        await limiter.check("agent", "search", now=0.0)
        assert limiter._buckets

        full_refill = limit.burst / limit.refill_per_second
        limiter._prune(full_refill)
        assert limiter._buckets == {}

    asyncio.run(exercise())


def test_an_unlimited_operation_is_allowed() -> None:
    limiter = TokenBucketLimiter()

    async def exercise() -> None:
        for _ in range(50):
            assert await limiter.check("agent", "not-configured", now=0.0) is None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("operation", "per_minute", "burst"),
    [
        ("search", 30, 10),
        ("get_note", 120, 30),
    ],
)
def test_limits_match_the_integration_spec(
    operation: str, per_minute: float, burst: int
) -> None:
    assert LIMITS[operation] == Limit(per_minute=per_minute, burst=burst)


def test_the_contribute_limit_diverges_from_the_spec_on_purpose() -> None:
    """The spec says 10/min burst 3; this ships 30/min burst 20.

    Asserted separately rather than folded into the spec-matching test above,
    because the point is that it is a decision and not drift. Contributions
    arrive in batches -- a librarian session settling nine notes, an importer
    replaying a corpus of fifty -- and burst 3 throttles every one of them
    without touching the abuse case, which is sustained rate. See the comment
    on LIMITS.
    """

    assert LIMITS["contribute"] == Limit(per_minute=30, burst=20)


def test_the_snapshot_limit_is_two_per_hour() -> None:
    # Expressed per-minute for one refill rule, so it is worth asserting the
    # spec's hourly figure survives the conversion.
    assert LIMITS["snapshot"].per_minute * 60 == pytest.approx(2.0)
    assert LIMITS["snapshot"].burst == 1


# --- The pre-authentication IP guard ---------------------------------------


def _guarded_app(limit: str) -> tuple[TestClient, list[int]]:
    """A router shaped like the vault's, with the database stood in for.

    `reached` counts how many requests got as far as the dependency that would
    have queried `vault_agent_credentials`. That count is the finding: without
    the guard it equals the number of requests sent, however many that is.
    """

    reached: list[int] = []

    async def authenticate() -> None:
        reached.append(1)

    guard = build_preauth_dependency(Limiter(key_func=client_ip), limit)
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/search", dependencies=[Depends(authenticate)])
    async def search() -> dict[str, bool]:
        return {"ok": True}

    async def handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc.detail)})

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/vault")
    app.add_exception_handler(RateLimitExceeded, handler)
    return TestClient(app), reached


def test_the_guard_refuses_before_the_credential_lookup() -> None:
    """The whole point of F1: a refused request must cost no database work.

    A route decorator would satisfy the 429 half of this and fail the second
    assertion, because FastAPI solves dependencies -- including the one that
    authenticates -- before it calls the endpoint the decorator wraps.
    """

    client, reached = _guarded_app("3/minute")

    statuses = [client.get("/api/v1/vault/search").status_code for _ in range(5)]

    assert statuses == [200, 200, 200, 429, 429]
    assert sum(reached) == 3


def test_the_guard_is_keyed_per_client_address() -> None:
    """One noisy caller must not spend another caller's allowance."""

    client, _reached = _guarded_app("2/minute")
    noisy = {"X-Forwarded-For": "203.0.113.9"}
    other = {"X-Forwarded-For": "198.51.100.4"}

    for _ in range(3):
        client.get("/api/v1/vault/search", headers=noisy)

    assert client.get("/api/v1/vault/search", headers=noisy).status_code == 429
    assert client.get("/api/v1/vault/search", headers=other).status_code == 200


def test_the_vault_router_carries_the_guard() -> None:
    """Attached to the router, so a route added later inherits it by default."""

    from app.vault.routes import router as vault_router

    assert any(
        dependency.dependency is enforce_preauth_ip_limit
        for dependency in vault_router.dependencies
    )


def test_the_client_key_is_the_leftmost_forwarded_address() -> None:
    """Heroku appends, so the original client is first and the proxies follow."""

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.9, 70.41.3.18, 150.172.238.178")],
            "client": ("10.0.0.1", 5000),
        }
    )

    assert client_ip(request) == "203.0.113.9"


def test_the_client_key_falls_back_to_the_socket_peer() -> None:
    """Local development has no proxy in front, so there is no header to read."""

    request = Request(
        {"type": "http", "headers": [], "client": ("127.0.0.1", 5000)}
    )

    assert client_ip(request) == "127.0.0.1"


def test_the_client_key_tolerates_a_missing_peer() -> None:
    """`request.client` is Optional in Starlette; a None key would raise."""

    request = Request({"type": "http", "headers": [], "client": None})

    assert client_ip(request) == "unknown"
