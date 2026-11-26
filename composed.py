"""The deployed application: the leaderboard and, when enabled, the vault.

Two independent ASGI applications share one process. ``ComposedApp`` sends each
request to the vault when one of the vault's routes matches its path, and to the
leaderboard otherwise, and runs both lifespans (host ADR 0016, vault ADR 0062).

It lives at the repository root rather than under ``app/`` so that nothing in
the leaderboard package imports the vault: ``tests/vault/test_boundaries.py``
holds that line. At extraction this module moves to ``knowledge-vault``
unchanged, apart from the vault's import path.

Run it with ``python run_dev.py`` locally, or ``gunicorn wsgi:app`` as the
Procfile does. ``app.main:app`` still serves the leaderboard on its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.routing import Match, Router
from starlette.types import ASGIApp, Receive, Scope, Send

from app.env import load_environment
from app.main import app as leaderboard_app


class ComposedApp:
    """Dispatch by path between the leaderboard and the vault.

    **The vault owns a request when any vault route matches its path**, fully or
    by path alone (a wrong method is still the vault's 405, not the
    leaderboard's SPA page). That is exactly what the single shared application
    did, where every vault route was registered ahead of the SPA catch-all. The
    ownership set is the vault's own route table, never a hand-kept prefix list,
    so a new vault route cannot be silently swallowed by the catch-all.

    Each application keeps its own middleware, exception handlers and state. In
    particular the leaderboard's CORS policy no longer wraps vault routes; no
    cross-origin browser calls them (host ADR 0016).

    Lifespans nest with the leaderboard outermost, so ``.env`` is loaded and
    Sentry is initialized before the vault starts, and the vault stops first.
    """

    def __init__(self, leaderboard: FastAPI, vault: FastAPI) -> None:
        self.leaderboard = leaderboard
        self.vault = vault
        # Starlette's Router implements the lifespan protocol; reusing it avoids
        # hand-writing the startup/shutdown message exchange.
        self._lifespan_router = Router(lifespan=self._lifespan_context)

    @asynccontextmanager
    async def _lifespan_context(self, _app: Any) -> AsyncIterator[None]:
        async with self.leaderboard.router.lifespan_context(self.leaderboard):
            async with self.vault.router.lifespan_context(self.vault):
                yield

    def owned_by_vault(self, scope: Scope) -> bool:
        return any(
            route.matches(scope)[0] is not Match.NONE
            for route in self.vault.router.routes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan_router(scope, receive, send)
            return
        target: ASGIApp = self.vault if self.owned_by_vault(scope) else self.leaderboard
        await target(scope, receive, send)


def create_composed_app() -> ASGIApp:
    """Return the leaderboard alone, or both applications behind one dispatcher.

    The decision is made at import, before any lifespan runs, so ``.env`` is
    loaded first. While the vault is disabled it is never imported, which
    ``tests/test_vault_disabled.py`` asserts.
    """

    load_environment()
    from app.vault.settings import vault_enabled

    if not vault_enabled():
        return leaderboard_app

    from app.vault.application import create_vault_app

    return ComposedApp(leaderboard=leaderboard_app, vault=create_vault_app())


app = create_composed_app()
