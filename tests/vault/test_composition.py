"""The composed application: two independent apps behind one dispatcher.

Host ADR 0016 replaced the single shared application with ``composed.py``: the
leaderboard and the vault are separate ASGI apps, and a request goes to the
vault when one of its routes matches the path. These tests pin the three things
that split could silently break: who owns which path, what the leaderboard's
CORS policy still covers, and the vault's 429 shape, which used to come from the
leaderboard's exception handler.

Composition-owned. At extraction this module moves to ``knowledge-vault``'s
CI, which runs it against HSS's public ``main`` (vault ADR 0062).
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from starlette.routing import Route

import composed
from app.main import _custom_rate_limit_handler
from app.main import app as leaderboard_app
from app.vault.application import create_vault_app, rate_limit_exceeded_handler


@pytest.fixture(scope="module")
def composed_app() -> composed.ComposedApp:
    if not isinstance(composed.app, composed.ComposedApp):
        pytest.skip("VAULT_ENABLED is off, so nothing is composed")
    return composed.app


def http_scope(method: str, path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/vault/search"),
        ("GET", "/api/v1/vault/notes"),
        ("GET", "/api/v1/vault/notes/0123456789abcdef0123456789abcdef"),
        # A wrong method is still the vault's 405, never the SPA's page.
        ("DELETE", "/api/v1/vault/search"),
        ("POST", "/api/v1/vault/mcp"),
        ("POST", "/api/v1/vault/mcp/"),
        ("GET", "/api/v1/vault/docs"),
        ("GET", "/authorize"),
        ("POST", "/token"),
        ("GET", "/vault/login"),
        ("GET", "/vault/browse"),
    ],
)
def test_vault_paths_go_to_the_vault(
    composed_app: composed.ComposedApp, method: str, path: str
) -> None:
    assert composed_app.owned_by_vault(http_scope(method, path))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/leaderboard"),
        ("GET", "/api/leaderboard/game_modes"),
        ("POST", "/api/auth/guest"),
        # The leaderboard's /api/auth/register is not the vault's /register.
        ("POST", "/api/auth/register"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/app/some/client/route"),
        ("GET", "/favicon.ico"),
    ],
)
def test_leaderboard_paths_go_to_the_leaderboard(
    composed_app: composed.ComposedApp, method: str, path: str
) -> None:
    assert not composed_app.owned_by_vault(http_scope(method, path))


def test_no_explicit_leaderboard_route_is_shadowed_by_the_vault(
    composed_app: composed.ComposedApp,
) -> None:
    """Every concrete leaderboard route still reaches the leaderboard.

    The single shared application registered the leaderboard's explicit routes
    first, so a collision would have gone the leaderboard's way. The dispatcher
    asks the vault first, so a collision now goes the other way. Asserting that
    none exists keeps the two orders equivalent.
    """

    shadowed: list[str] = []
    for route in leaderboard_app.router.routes:
        # Mounts (the SPA assets, the static root) are catch-alls by design;
        # only concrete routes can collide.
        if not isinstance(route, Route):
            continue
        method = sorted(route.methods or {"GET"})[0]
        concrete = re.sub(r"\{[^}]+\}", "x", route.path)
        if composed_app.owned_by_vault(http_scope(method, concrete)):
            shadowed.append(f"{method} {route.path}")

    assert shadowed == []


def test_the_leaderboard_app_registers_no_vault_routes() -> None:
    vault_like = [
        getattr(route, "path", "")
        for route in leaderboard_app.router.routes
        if getattr(route, "path", "").startswith(
            ("/api/v1/vault", "/vault", "/authorize", "/token")
        )
    ]

    assert vault_like == []


def test_cors_wraps_the_leaderboard_and_not_the_vault(
    composed_app: composed.ComposedApp, composed_client: TestClient
) -> None:
    """No cross-origin browser calls the vault, so its routes carry no CORS.

    The single application wrapped vault routes in the leaderboard's CORS
    middleware incidentally. Host ADR 0016 records that dropping it removes
    nothing anyone used.
    """

    preflight = {
        "Origin": "https://ericmay256.github.io",
        "Access-Control-Request-Method": "GET",
    }

    leaderboard = composed_client.options(
        "/api/leaderboard/game_modes", headers=preflight
    )
    vault = composed_client.options("/api/v1/vault/search", headers=preflight)

    assert (
        leaderboard.headers.get("access-control-allow-origin")
        == "https://ericmay256.github.io"
    )
    assert "access-control-allow-origin" not in vault.headers


def test_the_vault_renders_a_rate_limit_exactly_as_the_leaderboard_did() -> None:
    """The REST pre-auth guard raises ``RateLimitExceeded`` from a dependency.

    Inside the shared application the leaderboard's handler turned that into a
    429. The standalone vault must register its own, or a limited request
    becomes a 500. Same status, body and headers, so no client sees the split.
    """

    assert (
        create_vault_app().exception_handlers[RateLimitExceeded]
        is rate_limit_exceeded_handler
    )

    exc = SimpleNamespace(detail="600 per 1 minute", headers={"Retry-After": "60"})
    vault = asyncio.run(rate_limit_exceeded_handler(None, exc))  # type: ignore[arg-type]
    host = asyncio.run(_custom_rate_limit_handler(None, exc))  # type: ignore[arg-type]

    assert vault.status_code == host.status_code == 429
    assert json.loads(vault.body) == json.loads(host.body)
    assert vault.headers.get("retry-after") == host.headers.get("retry-after") == "60"
