from starlette.requests import Request

from app.limiter import get_real_ip


def request_with_forwarded_for(value: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", value.encode("ascii"))],
            "client": ("10.0.0.1", 1234),
        }
    )


def test_real_ip_uses_address_appended_by_heroku() -> None:
    request = request_with_forwarded_for(
        "198.51.100.7, 203.0.113.9"
    )

    assert get_real_ip(request) == "203.0.113.9"


def test_untrusted_forwarded_prefix_cannot_change_rate_limit_key() -> None:
    first = request_with_forwarded_for("198.51.100.1, 203.0.113.9")
    second = request_with_forwarded_for("198.51.100.2, 203.0.113.9")

    assert get_real_ip(first) == get_real_ip(second) == "203.0.113.9"


def test_real_ip_falls_back_to_socket_peer() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": ("192.0.2.4", 4321),
        }
    )

    assert get_real_ip(request) == "192.0.2.4"


def test_real_ip_handles_missing_socket_peer() -> None:
    request = Request({"type": "http", "headers": [], "client": None})

    assert get_real_ip(request) == "unknown"


def test_a_route_decorator_enforces_without_the_middleware() -> None:
    """SlowAPIMiddleware is not registered, and nothing depends on it.

    The middleware applies the Limiter's `default_limits`, which is empty, so
    it enforced nothing while reading as global rate limiting. Removing it
    must not weaken the per-route limits, which are the real gate -- so this
    builds the same wiring `create_app` uses, minus the middleware, and
    checks that a decorated route still refuses and still reports its headers.

    Its own Limiter rather than app.limiter: the suite sets
    RATE_LIMITER_ENABLED=false, so the shared one never refuses anything.
    """

    from fastapi import FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from starlette.responses import Response
    from starlette.testclient import TestClient

    from app.main import _custom_rate_limit_handler

    limiter = Limiter(
        key_func=lambda request: "fixed-key",
        storage_uri="memory://",
        enabled=True,
        headers_enabled=True,
    )

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)

    # `response: Response` is required, not decoration: with
    # headers_enabled=True the decorator injects the rate-limit headers into
    # that object, and raises without it. Every limited route in app/ takes
    # one, which is why they never depended on the middleware for headers.
    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request, response: Response) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        first = client.get("/limited")
        second = client.get("/limited")
        third = client.get("/limited")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429, "the decorator did not enforce its limit"
    assert "Rate limit exceeded" in third.json()["detail"]
    # headers_enabled=True is the Limiter's, not the middleware's.
    assert first.headers.get("x-ratelimit-limit") == "2"
