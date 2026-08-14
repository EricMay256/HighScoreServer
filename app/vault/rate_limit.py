"""Per-principal rate limiting for the vault surface.

slowapi lives in the host package and is unreachable from here — importing it
would breach the isolation rule that keeps extraction a directory move — so the
vault carries its own limiter. It is deliberately small: a token bucket per
(principal, operation), which is exactly the shape the integration spec states
its limits in ("sustained limit" is the refill rate, "burst" is the capacity).

**Keyed by authenticated principal, not IP.** Agents share egress addresses and
a credential is the thing an operator can actually revoke, so an IP key would
both over- and under-restrict.

**In-process, and that has a consequence worth stating.** Each Gunicorn worker
holds its own buckets, so a limit of 30/min admits up to 30 per worker per
minute. On a single-host deployment that is a known factor, not a surprise;
across hosts it stops being a limit at all, which is where a shared backend
becomes necessary rather than merely tidier.
"""

import asyncio
import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Limit:
    """A sustained rate with a burst allowance.

    ``per_minute`` is the long-run rate; ``burst`` is how much may arrive at
    once after an idle period. A bucket that has been idle refills to at most
    ``burst``, so bursts do not accumulate without bound.
    """

    per_minute: float
    burst: int

    @property
    def refill_per_second(self) -> float:
        return self.per_minute / 60.0


# From the integration spec's limits table, except `contribute` -- see below.
# The write, review, compile, and export operations are listed so that building
# those routes is adding a route rather than also inventing a quota.
#
# `contribute` deliberately diverges from the spec's 10/min burst 3. That shape
# assumes contributions trickle in, and they do not: they arrive as batches --
# a librarian session settling nine notes, an importer replaying a corpus of
# fifty. At burst 3 and a 6s refill every such batch is throttled end to end for
# no protective gain, since the batch is not the abuse case.
#
# What burst does and does not buy is worth being precise about. Long-run damage
# from a runaway loop is bounded by `per_minute` alone; `burst` only decides how
# fast the first few land. So a generous burst against a modest sustained rate
# costs little: a loop still cannot exceed 30 embedding calls per minute per
# principal per worker.
#
# Concurrency is bounded elsewhere and this does not change it. VAULT_DB_POOL_SIZE
# defaults to 1, so genuinely simultaneous contributions queue on one connection
# and fail on the 5s pool timeout rather than on this limiter. Raising the burst
# makes *sequential* batches fast; it does not make parallel contribution work,
# and a client that wants that needs a bigger pool first.
LIMITS: dict[str, Limit] = {
    "search": Limit(per_minute=30, burst=10),
    "get_note": Limit(per_minute=120, burst=30),
    "contribute": Limit(per_minute=30, burst=20),
    # An update costs what a contribution costs -- a dedup query and, when the
    # embedding text changed, an embedding call -- and arrives in the same
    # batches, so it gets the same shape. Its own bucket rather than sharing
    # contribute's, so a backfill cannot starve new contributions.
    "update": Limit(per_minute=30, burst=20),
    # Retirement is rare and irreversible, so it gets a deliberately tight
    # bucket: a loop that deletes is worse than a loop that writes.
    "retire": Limit(per_minute=10, burst=5),
    "snapshot": Limit(per_minute=2 / 60, burst=1),  # 2/hour
}


# Buckets are tiny and principals are few, so pruning is a housekeeping detail
# rather than a memory strategy. The threshold only keeps the scan off the hot
# path in the ordinary case.
_PRUNE_ABOVE = 256


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucketLimiter:
    """Token buckets keyed by (principal, operation).

    Idle buckets are pruned, but only when elapsed time *proves* they would
    have refilled to capacity — a bucket at capacity is indistinguishable from
    a fresh one, whereas dropping a partly-drained bucket would silently refund
    the requests it had already charged.
    """

    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(
        self,
        principal_id: str,
        operation: str,
        now: float | None = None,
    ) -> float | None:
        """Consume one token, or return the seconds to wait.

        None means allowed. A float is the ``Retry-After`` value, always at
        least 1 because a sub-second Retry-After rounds to 0 and invites an
        immediate retry.
        """

        limit = LIMITS.get(operation)
        if limit is None:
            # An operation with no configured quota is not silently unlimited
            # by accident; it is unlimited because nobody gave it a limit, and
            # that should be visible in this dict rather than here.
            return None

        moment = time.monotonic() if now is None else now
        key = (principal_id, operation)

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit.burst), updated_at=moment)

            elapsed = max(0.0, moment - bucket.updated_at)
            bucket.tokens = min(
                float(limit.burst),
                bucket.tokens + elapsed * limit.refill_per_second,
            )
            bucket.updated_at = moment

            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            self._buckets[key] = bucket

            if len(self._buckets) > _PRUNE_ABOVE:
                self._prune(moment)

            if allowed:
                return None
            deficit = 1.0 - bucket.tokens
            return max(1.0, deficit / limit.refill_per_second)

    def _prune(self, moment: float) -> None:
        """Drop buckets that have provably refilled to capacity.

        Called only when the dict has grown, because it is a scan. Correctness
        does not depend on it running: a retained full bucket behaves exactly
        like an absent one.
        """

        stale = [
            key
            for key, bucket in self._buckets.items()
            if (limit := LIMITS.get(key[1])) is not None
            and moment - bucket.updated_at >= limit.burst / limit.refill_per_second
        ]
        for key in stale:
            del self._buckets[key]

    def reset(self) -> None:
        """Drop all buckets. For tests and for a process that has just started."""

        self._buckets.clear()


_limiter = TokenBucketLimiter()


def get_limiter() -> TokenBucketLimiter:
    """The process-wide limiter.

    A module-level instance rather than app state because the limiter is
    per-process by construction; putting it on the app would imply it is shared
    when it is not.
    """

    return _limiter
