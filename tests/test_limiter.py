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
