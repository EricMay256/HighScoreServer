"""Token-bucket behaviour, driven by an injected clock rather than sleeping."""

import asyncio

import pytest

from app.vault.rate_limit import LIMITS, Limit, TokenBucketLimiter


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
        ("contribute", 10, 3),
    ],
)
def test_limits_match_the_integration_spec(
    operation: str, per_minute: float, burst: int
) -> None:
    assert LIMITS[operation] == Limit(per_minute=per_minute, burst=burst)


def test_the_snapshot_limit_is_two_per_hour() -> None:
    # Expressed per-minute for one refill rule, so it is worth asserting the
    # spec's hourly figure survives the conversion.
    assert LIMITS["snapshot"].per_minute * 60 == pytest.approx(2.0)
    assert LIMITS["snapshot"].burst == 1
