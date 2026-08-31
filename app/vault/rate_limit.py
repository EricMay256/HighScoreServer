"""Rate limiting for the vault surface, in two layers that answer two questions.

**The quota** is a token bucket per (principal, operation), which is exactly the
shape the integration spec states its limits in ("sustained limit" is the refill
rate, "burst" is the capacity). It enforces what an operator granted a
credential, so it is keyed by authenticated principal, not IP: agents share
egress addresses and a credential is the thing an operator can actually revoke.

**The pre-auth guard** is IP-keyed and charged before the credential is looked
up. It exists because the quota structurally cannot cover the cost of
authentication itself — see `enforce_preauth_ip_limit` below. Neither layer
replaces the other, and removing either reopens a hole the other never covered.

**Both are per process by default, and that has a consequence worth stating.**
Each Gunicorn worker holds its own state, so a limit of 30/min admits up to 30
per worker per minute. On a single-host deployment that is a known factor, not a
surprise; across hosts it stops being a limit at all, which is where a shared
backend becomes necessary rather than merely tidier. The pre-auth guard can take
one today via ``VAULT_RATE_LIMIT_STORAGE_URI``, because it is the layer where an
attacker is least likely to stay on one worker.

slowapi appears here for the pre-auth guard only. That is a **third-party**
import, not a host import: `app/vault/` still contains no `from app.`, so
extraction remains a directory move, and `tests/vault/test_boundaries.py` still
passes. The cost is that slowapi leaves with the package, and the extraction
manifest lists it.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import JSONResponse


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
# defaults to 2 and a request checks out twice in sequence, so a handful of
# simultaneous contributions still queue and can fail on the 5s pool timeout
# rather than on this limiter. Raising the burst makes *sequential* batches
# fast; it does not make wide parallel contribution work, and a client that
# wants that needs a bigger pool first.
LIMITS: dict[str, Limit] = {
    # One row and no embedding, called once when a console signs in or renews.
    # Generous because refusing it costs a header rather than protecting
    # anything, and tight enough that it is not a free polling loop.
    "authorization": Limit(per_minute=60, burst=20),
    "search": Limit(per_minute=30, burst=10),
    "get_note": Limit(per_minute=120, burst=30),
    "contribute": Limit(per_minute=30, burst=20),
    # Proposals persist untrusted workflow state but do not embed or mutate the
    # corpus. A distinct bucket matches the distinct OAuth capability.
    "amendment_propose": Limit(per_minute=30, burst=20),
    # An update costs what a contribution costs -- a dedup query and, when the
    # embedding text changed, an embedding call -- and arrives in the same
    # batches, so it gets the same shape. Its own bucket rather than sharing
    # contribute's, so a backfill cannot starve new contributions.
    "update": Limit(per_minute=30, burst=20),
    # Filling in an omitted summary costs an embedding call and a dedup query,
    # so it is priced like the update it is a narrow slice of. Its own bucket
    # because it is the *repair* for a contribution: sharing contribute's would
    # mean a batch of notes could exhaust the allowance that describes them.
    "set_summary": Limit(per_minute=30, burst=20),
    # Retirement is rare and irreversible, so it gets a deliberately tight
    # bucket: a loop that deletes is worse than a loop that writes.
    "retire": Limit(per_minute=10, burst=5),
    # Review is a human at a queue, not an agent in a loop. Listing and reading
    # are generous enough to page through a backlog; deciding is as tight as
    # retiring, because an accepted case publishes content and a rejected one
    # destroys it.
    "review_list": Limit(per_minute=60, burst=20),
    "review_read": Limit(per_minute=60, burst=20),
    "review_decide": Limit(per_minute=10, burst=5),
    "amendment_list": Limit(per_minute=60, burst=20),
    "amendment_read": Limit(per_minute=60, burst=20),
    "amendment_decide": Limit(per_minute=10, burst=5),
    # Compilation is a librarian loop: one plan, a burst of pages, one finish.
    # Planning is deliberately tight -- it opens a run row every time, and a
    # loop that plans without finishing accumulates `running` runs nobody
    # settles. Writing pages gets contribute's shape, because that is what it
    # is: a batch of synthesis arriving together, each costing an embedding
    # call. Settling is as cheap as planning is expensive and needs no headroom
    # beyond what a retry wants.
    "compile_plan": Limit(per_minute=6, burst=3),
    "compile_write": Limit(per_minute=30, burst=20),
    "compile_settle": Limit(per_minute=10, burst=5),
    "snapshot": Limit(per_minute=2 / 60, burst=1),  # 2/hour
}


# Per-principal overrides, for workloads whose shape differs from the one the
# table above is sized for.
#
# The default limits describe an *interactive agent*: retrieve a little, think,
# contribute one note. A bulk import is a different animal -- it is a queue of
# known-good content drained as fast as the write path allows -- and at 30/min
# a 500-note corpus takes over four hours, which is not a safety property,
# just friction.
#
# Deliberately code rather than configuration, for the same reason unknown
# operations fail closed: an environment variable that widens a quota is a way
# to unlimit production by accident, and this is a considered grant to one
# named principal rather than a knob.
#
# 'importer' is that principal by documented convention -- HANDOFF requires the
# importer run under it because vault_write_requests is keyed
# (principal_id, idempotency_key), so a different name silently bypasses the
# only duplicate guard. That requirement is what makes the name safe to key on
# here.
#
# Read operations are untouched. Import writes; it does not search.
PRINCIPAL_LIMITS: dict[str, dict[str, Limit]] = {
    "importer": {
        "contribute": Limit(per_minute=300, burst=60),
        # Re-import and backfill replace rather than insert (ADR 0018), so the
        # update path carries the same bulk shape and needs the same headroom.
        "update": Limit(per_minute=300, burst=60),
    },
}


def limit_for(principal_id: str, operation: str) -> Limit:
    """The quota governing one principal's use of one operation.

    An override never *invents* an operation: the base table is still the
    single statement of which operations exist, and an unregistered one raises
    here exactly as it did before. Widening a known quota for a known principal
    is the only thing this can do.
    """

    limit = LIMITS.get(operation)
    if limit is None:
        raise ValueError(f"Unknown vault quota operation: {operation}")
    return PRINCIPAL_LIMITS.get(principal_id, {}).get(operation, limit)


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

        limit = limit_for(principal_id, operation)

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
        does not depend on it *running*: a retained full bucket behaves exactly
        like an absent one. It does depend on this asking the same question
        ``check`` did -- ``limit_for``, not ``LIMITS`` -- because a bucket is
        charged against the effective quota and must be forgiven on the same
        one.

        Reading the base table here was safe only by arithmetic accident. The
        shipped override widens burst 3x and rate 10x, so its bucket refills in
        12s where the base takes 40s, and pruning late merely retains it. An
        override that raised burst faster than rate would invert that: at
        ``per_minute=60, burst=200`` a bucket refills in 200s while this would
        drop it at 40s, one-fifth full, silently refunding the 160 requests it
        had already charged. That is exactly the refund the class docstring
        promises not to make, so the guard belongs in the code rather than in
        the choice of constants.

        An operation absent from ``LIMITS`` is skipped rather than raised on:
        ``check`` resolves ``limit_for`` before it creates a bucket, so such a
        key cannot exist, and housekeeping must not be the thing that turns an
        impossible state into a failed request.
        """

        stale = [
            key
            for key, bucket in self._buckets.items()
            if key[1] in LIMITS
            and moment - bucket.updated_at
            >= (limit := limit_for(*key)).burst / limit.refill_per_second
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


def client_ip(request: Request) -> str:
    """Return the client address observed by the trusted Heroku router hop.

    Heroku appends the address it observes to the **right** of any caller-supplied
    ``X-Forwarded-For`` entries. The rightmost value is therefore the only useful
    limiter key in this direct-to-Heroku topology; trusting the leftmost value
    lets a caller mint a new bucket by changing an untrusted prefix. If another
    proxy is placed in front of Heroku, this assumption must be revisited because
    the rightmost value would identify that proxy instead of the original client.
    """

    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.rsplit(",", maxsplit=1)[-1].strip()
    client = request.client
    return client.host if client is not None else "unknown"


# Deliberately loose, and sustained-only rather than also per-second. This is a
# floor, not a quota: the per-principal buckets above are where real limits are
# expressed, and a caller legitimately holding several credentials behind one
# egress address can exceed any tight IP number without doing anything wrong
# (get_note alone is 120/min per principal). A short-window clause would also
# make the test suite order-dependent, since every test shares 127.0.0.1.
#
# What it has to achieve is narrower than it looks: turning "unbounded database
# round trips from an anonymous caller" into "a bounded number", after which
# hammering the endpoint stops being interesting. Tighten it via the environment
# for a deployment that expects fewer clients.
_DEFAULT_PREAUTH_LIMIT = "600/minute"


def _make_ip_limiter() -> Limiter:
    """Build the pre-auth limiter.

    ``in_memory_fallback_enabled`` so an unreachable Redis degrades this layer
    to per-process instead of failing vault requests — the same tradeoff the
    host makes for its own limiter, reached through slowapi's own mechanism
    rather than a connectivity probe at import.

    ``headers_enabled`` stays off. The rate-limit headers describe a quota the
    caller can act on, and this is not that; the per-principal 429 is the one
    carrying Retry-After.
    """

    return Limiter(
        key_func=client_ip,
        storage_uri=os.environ.get("VAULT_RATE_LIMIT_STORAGE_URI", "memory://"),
        in_memory_fallback_enabled=True,
        headers_enabled=False,
    )


# Module level because the decorator below needs it at import time. Reading the
# environment here means these two settings are fixed for the life of the
# process, which matches how the limiter itself is scoped.
_ip_limiter = _make_ip_limiter()
_PREAUTH_LIMIT = os.environ.get(
    "VAULT_PREAUTH_RATE_LIMIT",
    _DEFAULT_PREAUTH_LIMIT,
)


def build_preauth_dependency(
    limiter: Limiter,
    limit: str,
) -> Callable[[Request], Awaitable[None]]:
    """Build the FastAPI dependency that charges the pre-auth guard.

    **A dependency, not a route decorator, and the distinction is the whole
    point.** FastAPI resolves dependencies before it calls the endpoint, and
    authentication is a dependency — `_authenticated` opens a vault transaction
    and queries `vault_agent_credentials`. A `@limiter.limit` decorator on the
    endpoint therefore charges its token *after* the database round trip it was
    meant to prevent, which is no protection at all.

    Attached at the router so it covers the whole surface including routes not
    written yet, and so it is solved before the per-route dependencies that
    authenticate. A route that opts out has to say so explicitly.

    A factory rather than a bare decorated function so a test can build one at a
    limit it can actually exhaust; the module-level instance below is the real
    one. slowapi requires the wrapped callable to take a parameter named
    ``request``, and raises at import if it does not.
    """

    @limiter.limit(limit)
    async def guard(request: Request) -> None:
        # Called for its effect. Raising RateLimitExceeded is the *successful*
        # path once the limit is spent; the host application's existing handler
        # turns it into a 429.
        return None

    return guard


enforce_preauth_ip_limit = build_preauth_dependency(_ip_limiter, _PREAUTH_LIMIT)


def reset_ip_limiter() -> None:
    """Drop all pre-auth buckets. For tests and for a freshly started process.

    The quota's buckets are keyed per principal, so a test issuing a new
    credential gets a clean bucket for free. An IP key has no such escape
    hatch — every test shares a loopback address — so this is the seam that
    keeps the suite order-independent.
    """

    _ip_limiter.reset()


# The login POST's own bucket, far tighter than the pre-auth guard.
#
# A public, unauthenticated password endpoint is a brute-force target, and the
# 600/minute IP guard is sized for "bound the cost of authentication", not for
# "bound guesses at a password". Three defences stack here and none replaces
# another: bcrypt's cost factor makes each guess expensive, this bucket makes
# them rare, and the login route redeems the nonce whatever the password turns
# out to be, so a wrong guess burns the authorization.
#
# The nonce is redeemed in the same transaction that mints the code, which is
# *after* bcrypt rather than before it -- so concurrent submits on one nonce can
# each get a password evaluation, and only one of them redeems. This bucket is
# what bounds that, which is the reason it is not merely defence in depth.
#
# 10/minute is generous for a person typing one password and hostile to anything
# else. Keyed by IP like the guard it sits beside, because there is no
# authenticated principal at a login form -- that is what the form is for.
_DEFAULT_LOGIN_LIMIT = "10/minute"


def login_rate_limit() -> str:
    """The configured login limit, read when the routes are built.

    At call time rather than at import, unlike ``_PREAUTH_LIMIT`` above. That
    one has to be module-level because its decorator is applied at import; this
    one is applied while the application is being constructed, which is late
    enough to read the environment and early enough to be fixed for the life of
    the app. It is also what lets a test build an app at a limit it can actually
    exhaust without reloading this module -- and reloading it would replace the
    limiter instance other modules already hold.
    """

    return os.environ.get("VAULT_LOGIN_RATE_LIMIT", _DEFAULT_LOGIN_LIMIT)


_DEFAULT_REGISTRATION_LIMIT = "10/minute"


def registration_rate_limit() -> str:
    """The configured `/register` limit, read when the routes are built."""

    return os.environ.get(
        "VAULT_REGISTRATION_RATE_LIMIT",
        _DEFAULT_REGISTRATION_LIMIT,
    )


def build_registration_guard() -> Callable[[Request], Awaitable[None]]:
    """Charge `/register`'s own bucket, far tighter than the 600/min guard.

    Registration is public, unauthenticated, and *writes a row*. One client
    registers once, so a tight limit costs an honest caller nothing, while the
    general guard is sized for reads and would let a single IP add hundreds of
    thousands of rows a day.

    Defence in depth and not the storage bound -- a limit slows accumulation
    rather than capping it. The bound is that registrations are pruned and that
    nothing permanent is written per call (see `oauth.register_client`).

    The inner function is named rather than reusing ``build_preauth_dependency``
    because slowapi scopes a bucket by the wrapped callable's qualified name:
    two guards built from that factory are both
    ``build_preauth_dependency.<locals>.guard`` and would silently share one
    bucket, which is the opposite of what a dedicated limit is for.
    """

    @_ip_limiter.limit(registration_rate_limit())
    async def registration_guard(request: Request) -> None:
        return None

    return registration_guard


def get_login_limiter() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The decorator that charges the login bucket.

    Returns slowapi's decorator rather than a dependency, unlike
    ``build_preauth_dependency``: the login routes are Starlette ``Route``
    objects sitting beside the SDK's, so there is no FastAPI dependency system
    to attach to. Decorating the endpoint is correct *here* for the reason it
    was wrong there -- this handler does no database work before the limiter
    charges, so there is nothing to protect that runs first.

    Its own bucket rather than the guard's because slowapi scopes by endpoint,
    so the two never draw down each other's allowance.
    """

    return _ip_limiter.limit(login_rate_limit())


def reset_login_limiter() -> None:
    """Drop the login buckets. Shares storage with the pre-auth guard.

    ``reset_ip_limiter`` already clears everything the limiter holds, including
    these; this exists so a test can say what it means.
    """

    _ip_limiter.reset()


def guard_asgi_app(app: Any, charge: Any = None) -> Any:
    """Charge the pre-auth IP guard in front of an ASGI application.

    The third shape this guard has to take, and the reason is structural. As a
    FastAPI dependency it covers the vault router; as a call inside
    ``VaultMCPAuthMiddleware`` it covers the mount. The OAuth routes are
    root-mounted Starlette ``Route`` objects, so they inherit **neither** -- and
    the SDK hands back endpoints that are ``CORSMiddleware`` instances rather
    than ``async def(request)``, so slowapi's decorator cannot wrap them either.
    Wrapping the ASGI callable is what works on all of them.

    Without this, ``/register``, ``/authorize``, ``/token`` and ``/revoke`` are
    unauthenticated endpoints with no limit at all -- and `/register` writes a
    row. ADR 0024 says the guard "must cover them, for the reason it covers the
    MCP mount: without it they are the only unbounded door", which had not
    actually been implemented.

    ``charge`` selects which bucket is spent, defaulting to the pre-auth guard.
    `/register` passes its own, tighter one -- the same arrangement the login
    POST has, where a dedicated bucket replaces the general allowance rather
    than stacking on it.

    Renders 429 the same way the MCP mount does, and for the same reason: the
    host's slowapi handler does not see exceptions raised inside a wrapped
    route, and an operator reading a 429 should not be able to tell which
    surface produced it.
    """

    charged = enforce_preauth_ip_limit if charge is None else charge

    async def guarded(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        try:
            await charged(request=request)
        except RateLimitExceeded as exc:
            await JSONResponse(
                {"detail": f"Rate limit exceeded: {exc.detail}"},
                status_code=429,
                headers=dict(exc.headers) if exc.headers else None,
            )(scope, receive, send)
            return
        await app(scope, receive, send)

    return guarded
