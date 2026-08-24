"""Noticing when `VAULT_PUBLIC_URL` stops describing the deployment.

The variable is configuration on purpose -- the routes are built before any
request exists, and deriving an issuer from the `Host` header would let a
forged header point `token_endpoint` at somebody else's server. What
configuration cannot do is notice it has gone stale, and a stale value
publishes discovery documents that send clients somewhere wrong.

The failure surfaces at the *client*, as "this server does not support OAuth",
which is a long way from its cause. So the server says so itself.
"""

import logging

import pytest
from starlette.requests import Request

from app.vault.public_url import (
    configured_public_url,
    observed_origin,
    report_public_url_drift,
    reset_public_url_report,
)


CONFIGURED = "https://high-score-server.example.com"


def _request(host: str, *, proto: str | None = "https") -> Request:
    headers = [(b"host", host.encode())]
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/.well-known/oauth-authorization-server",
            "headers": headers,
            "scheme": "http",
            "server": ("localhost", 80),
            "query_string": b"",
        }
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", CONFIGURED)
    reset_public_url_report()
    yield
    reset_public_url_report()


def test_a_matching_host_is_reported_without_alarm(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        report_public_url_drift(_request("high-score-server.example.com"))

    records = [r for r in caplog.records if r.name == "app.vault.public_url"]
    assert [r.levelno for r in records] == [logging.INFO]


def test_a_mismatched_host_warns_and_names_both(caplog) -> None:
    """Both values, because either one could be the wrong one.

    A custom domain in front of the app makes the *configured* value stale; a
    renamed app or a stray proxy makes the observed one a surprise. The line
    has to be readable by somebody who does not yet know which.
    """

    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        report_public_url_drift(_request("vault.someone-elses-domain.test"))

    records = [r for r in caplog.records if r.name == "app.vault.public_url"]
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert CONFIGURED in message
    assert "https://vault.someone-elses-domain.test" in message
    # The configured value still wins, and the message has to say so -- an
    # operator reading this must not conclude the server switched hosts.
    assert "still what is published" in message


def test_it_reports_once_per_process(caplog) -> None:
    """A deployment property, not a per-request one.

    Logged on every request it would be noise on a healthy server and no more
    informative on a broken one.
    """

    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        for _ in range(5):
            report_public_url_drift(_request("vault.someone-elses-domain.test"))

    records = [r for r in caplog.records if r.name == "app.vault.public_url"]
    assert len(records) == 1


def test_nothing_is_said_when_the_variable_is_unset(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable from the OAuth routes, which do not exist without it.

    Guarded anyway: the alternative is a warning comparing the observed origin
    against the empty string, which reads as a misconfiguration that is not one.
    """

    monkeypatch.delenv("VAULT_PUBLIC_URL", raising=False)
    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        report_public_url_drift(_request("anything.test"))

    assert [r for r in caplog.records if r.name == "app.vault.public_url"] == []


@pytest.mark.parametrize(
    ("host", "proto", "expected"),
    [
        ("Example.TEST", "https", "https://example.test"),
        ("example.test:8443", "https", "https://example.test:8443"),
        # Heroku's router terminates TLS, so the app sees http on the socket and
        # the real scheme only in the header. Without this the comparison would
        # warn on every healthy deployment.
        ("example.test", None, "http://example.test"),
        # Chained proxies append; the first entry is the client-facing one.
        ("example.test", "https,http", "https://example.test"),
    ],
)
def test_the_observed_origin_is_normalised(
    host: str, proto: str | None, expected: str
) -> None:
    assert observed_origin(_request(host, proto=proto)) == expected


def test_a_request_without_a_host_header_is_not_a_mismatch(caplog) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "scheme": "https",
            "server": ("localhost", 443),
            "query_string": b"",
        }
    )
    assert observed_origin(request) is None

    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        report_public_url_drift(request)

    records = [r for r in caplog.records if r.name == "app.vault.public_url"]
    assert [r.levelno for r in records] == [logging.INFO]


def test_the_trailing_slash_is_not_a_mismatch(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main.py` strips it before building routes; this must agree.

    Otherwise setting the variable with a trailing slash -- which the docs warn
    against and somebody will still do -- warns about a deployment that is fine.
    """

    monkeypatch.setenv("VAULT_PUBLIC_URL", CONFIGURED + "/")
    assert configured_public_url() == CONFIGURED

    with caplog.at_level(logging.INFO, logger="app.vault.public_url"):
        report_public_url_drift(_request("high-score-server.example.com"))

    records = [r for r in caplog.records if r.name == "app.vault.public_url"]
    assert [r.levelno for r in records] == [logging.INFO]


def test_every_public_oauth_route_reports_drift() -> None:
    """The check is useless if it is not wired to anything.

    Asserted on the assembly rather than by driving a request, because the one
    thing that would make a runtime test pass trivially -- a process that has
    already reported -- is also the normal state. Two properties matter and both
    are here: the observation is applied to every route, and the rate-limit
    guard sits *outside* it, so a refused request costs nothing else.
    """

    import inspect

    from app.vault import oauth_routes

    source = inspect.getsource(oauth_routes.build_vault_oauth_routes)
    assert "_observed(" in source, "the drift check is not wired into the routes"

    guarded = source.index("_guarded(")
    observed = source.index("_observed(")
    assert guarded < observed, "the guard must wrap the observation, not vice versa"
