"""The vault as a standalone ASGI application.

Everything the host used to wire into its own app lives here: the engine and
embedding lifespan, the REST router and its exception handlers, the MCP mount,
and the OAuth and console routes. A composer (the repository's ``composed.py``
today, the ``knowledge-vault`` deployment after extraction; host ADR 0016 and
vault ADR 0062) runs this app beside the leaderboard and routes each request to
whichever app owns its path.

Paths are absolute -- ``/api/v1/vault/...``, ``/authorize``, ``/vault/browse``
-- so a composer forwards the ASGI scope unchanged and nothing here needs a
``root_path``.

This module reads the process environment and never loads ``.env``: that is the
launcher's job, and the host's loader is not importable from this package.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


VAULT_API_PREFIX = "/api/v1/vault"
VAULT_MCP_PATH = f"{VAULT_API_PREFIX}/mcp"


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Render a slowapi limit as a 429, in the shape the host always produced.

    The REST router's pre-auth guard raises ``RateLimitExceeded`` from a
    dependency. While the vault shared the host's application, the host's own
    handler turned that into a 429; nothing in this package did. A standalone
    app without this handler answers a limited request with a 500. The body and
    headers match the host's handler byte for byte, so no client sees the split.
    """

    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
        headers=dict(exc.headers) if exc.headers else {},
    )


@asynccontextmanager
async def vault_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the vault's runtime, in the order the host used.

    The engine first: a provider with no corpus to search is not a useful thing
    to have running. ``report_vault_pool`` wraps the MCP lifespan so its
    high-water line lands after everything that could check out a connection has
    stopped. ``vault_mcp_lifespan`` is required because a mount does not run the
    mounted app's lifespan, and without it every tool call fails on a task group
    that was never entered.
    """

    from .db import close_vault_db, init_vault_db, report_vault_pool
    from .embedding_runtime import close_vault_embeddings, init_vault_embeddings
    from .mcp import vault_mcp_lifespan

    try:
        await init_vault_db()
        await init_vault_embeddings()
        async with report_vault_pool(), vault_mcp_lifespan(app.state.vault_mcp_app):
            yield
    finally:
        await close_vault_embeddings()
        await close_vault_db()


def create_vault_app() -> FastAPI:
    """Build the vault application. Route registration happens here, lifespan later.

    The API docs live under the vault prefix rather than at FastAPI's default
    ``/docs``: a composer routes by path, so a vault-owned ``/docs`` would shadow
    the leaderboard's.
    """

    from .mcp import build_vault_mcp_app
    from .routes import router as vault_router
    from .routes import vault_saturation_handler

    app = FastAPI(
        title="Knowledge Vault API",
        lifespan=vault_lifespan,
        docs_url=f"{VAULT_API_PREFIX}/docs",
        redoc_url=None,
        # FastAPI's default is /docs/oauth2-redirect, which would shadow the
        # leaderboard's. tests/vault/test_composition.py found it.
        swagger_ui_oauth2_redirect_url=f"{VAULT_API_PREFIX}/docs/oauth2-redirect",
        openapi_url=f"{VAULT_API_PREFIX}/openapi.json",
    )
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(SQLAlchemyTimeoutError, vault_saturation_handler)
    app.include_router(vault_router, prefix=VAULT_API_PREFIX)

    # Registered before the mount so the exact path matches here. A mount
    # answers only its trailing-slash form, and the bare form would fall
    # through to a bare 405 -- which is precisely the URL an operator types
    # when configuring a client. 307 preserves the method and body, so a
    # POSTed JSON-RPC call survives the hop. It does no work and touches no
    # credential, so running ahead of the pre-auth guard costs nothing.
    @app.api_route(
        VAULT_MCP_PATH, methods=["GET", "POST", "DELETE"], include_in_schema=False
    )
    async def vault_mcp_canonical_path() -> RedirectResponse:
        return RedirectResponse(f"{VAULT_MCP_PATH}/", status_code=307)

    # Held on app state, not in a module-level cache: the transport's session
    # manager refuses a second run() on the same instance, so each application
    # must own its own. The mount carries its own authentication and pre-auth
    # guard, because a mount inherits neither the router's dependencies nor this
    # app's exception handlers. See mcp.py.
    app.state.vault_mcp_app = build_vault_mcp_app()
    app.mount(VAULT_MCP_PATH, app.state.vault_mcp_app)

    # The OAuth authorization server (ADR 0024) is root-mounted because RFC
    # 9728 fixes the protected-resource metadata path relative to the host, and
    # the SDK builds /authorize and friends from issuer_url. Gated on
    # VAULT_PUBLIC_URL: a deployment that cannot state its own origin cannot
    # serve correct discovery metadata, and advertising an authorization server
    # before one answers is worse than a bare 401. The landing page and both
    # consoles authorize through those routes, so they share the gate
    # (ADRs 0037, 0039).
    public_url = (os.environ.get("VAULT_PUBLIC_URL") or "").rstrip("/")
    if public_url:
        from .browse_console import build_vault_browse_routes
        from .landing_page import build_vault_landing_routes
        from .oauth_routes import build_vault_oauth_routes
        from .review_console import build_vault_review_routes

        app.router.routes.extend(
            build_vault_oauth_routes(
                issuer_url=public_url,
                mcp_url=f"{public_url}{VAULT_MCP_PATH}",
            )
        )
        app.router.routes.extend(build_vault_landing_routes())
        app.router.routes.extend(build_vault_review_routes())
        app.router.routes.extend(build_vault_browse_routes())

    return app
