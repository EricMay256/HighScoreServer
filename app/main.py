import logging
import os
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app import spa_routes
from app.auth_routes import router as auth_router
from app.cache import close_cache, init_cache
from app.db import close_db, init_db
from app.env import load_environment, validate_environment
from app.leaderboard_routes import CrossRouteError
from app.leaderboard_routes import router as leaderboard_router
from app.limiter import limiter
from app.vault.settings import vault_enabled
from app.view_routes import router as view_router


logger = logging.getLogger(__name__)

# Browser origins allowed to call public leaderboard GET endpoints.
#
# Keep this list hardcoded and small on purpose. CORS is not an auth boundary;
# it documents expected browser callers and helps surface unexpected origins
# during development.
CORS_ALLOWED_ORIGINS = (
    "https://ericmay256.github.io",   # production portfolio
    "http://localhost:8080",          # local portfolio preview (python -m http.server)
    "http://127.0.0.1:8080",          # same, IPv4 literal — some browsers normalize differently
    "http://localhost:5500",          # local portfolio preview (VS Code "Go Live" extension)
    "http://127.0.0.1:5500",          # same, IPv4 literal — some browsers normalize differently
)

async def _custom_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
        headers=dict(exc.headers) if exc.headers else {},
    )


async def _cross_route_handler(request: Request, exc: CrossRouteError) -> JSONResponse:
    # 409 with `detail` as a human-readable string plus machine-routable `code`
    # and `submit_to` as top-level siblings (not nested inside `detail`) — the
    # shape the hss-unity client parses cleanly. A bare HTTPException can only
    # emit `{"detail": ...}`, hence this dedicated handler.
    return JSONResponse(
        status_code=409,
        content={"detail": exc.detail, "code": exc.code, "submit_to": exc.submit_to},
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()
    validate_environment()

    dsn = os.environ.get("SENTRY_DSN")
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[
                StarletteIntegration(),
                FastApiIntegration(),
            ],
            traces_sample_rate=0.2,  # capture 20% of requests as performance traces
            send_default_pii=False,  # don't ship request headers/bodies by default
        )

    vault_is_enabled = False
    await init_db()
    try:
        init_cache()
        vault_is_enabled = vault_enabled()
        if vault_is_enabled:
            from app.vault.db import (
                close_vault_db,
                init_vault_db,
                report_vault_pool,
            )
            from app.vault.embedding_runtime import (
                close_vault_embeddings,
                init_vault_embeddings,
            )
            from app.vault.mcp import vault_mcp_lifespan

            await init_vault_db()
            # Only after the engine is up: a provider with no corpus to search
            # is not a useful thing to have running.
            await init_vault_embeddings()

            # Two context managers, both required and both easy to omit:
            #
            # report_vault_pool logs this worker's connection high-water mark on
            # the way out, which is the evidence the vault-enablement review
            # asks for and cannot be reconstructed after the process exits. It
            # wraps the inner one so its closing line lands after everything
            # that could check out a connection has stopped.
            #
            # vault_mcp_lifespan starts the MCP transport's session manager. A
            # mount does not run the mounted app's lifespan, so without this
            # every tool call fails on a task group that was never entered.
            async with report_vault_pool(), vault_mcp_lifespan(
                app.state.vault_mcp_app
            ):
                yield
        else:
            yield
    finally:
        if vault_is_enabled:
            await close_vault_embeddings()
            await close_vault_db()
        await close_db()
        await close_cache()


def create_app() -> FastAPI:
    # Route registration is decided here, before lifespan runs, so .env has to
    # be loaded now for VAULT_ENABLED to be visible. load_environment is cached
    # and uses override=False, so the later lifespan call is a no-op and the
    # process environment still wins over .env.
    load_environment()

    app = FastAPI(title="Leaderboard API", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    app.add_exception_handler(CrossRouteError, _cross_route_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Register CORS after SlowAPI (Starlette uses reverse registration order)
    # so it runs first on requests and handles preflight OPTIONS before any
    # global rate-limit accounting. Per-route limits are applied at the
    # decorator, but ordering CORS outermost also ensures CORS headers are
    # present on 429 and 5xx responses from inner middleware.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ALLOWED_ORIGINS),
        # Browser CORS is for the public leaderboard/auth surfaces. Vault
        # writers are operator-issued machine clients, so PUT/DELETE are
        # intentionally absent; see the vault configuration guide.
        # OPTIONS not required but included for clarity.
        allow_methods=["GET", "OPTIONS", "POST"],
        # With allow_credentials=False the browser won't attach cookies on cross-origin
        # requests, so a permissive allow_headers can't be combined with ambient auth
        # to exfiltrate user data. Bearer tokens are sent only by clients that already
        # possess them, so allowing "*" here is safe for this API.
        allow_headers=["*"],
        # allow_credentials=False is intentional: identity is provided, if needed,
        # through bearer tokens.
        allow_credentials=False,
        max_age=600,  # cache preflight for 10 min — keeps repeat fetches snappy
    )

    # 1. View (Jinja2) routes — no prefix
    app.include_router(view_router)
    # 2. Leaderboard routes
    app.include_router(leaderboard_router, prefix="/api/leaderboard")
    # 3. Authentication routes
    app.include_router(auth_router, prefix="/api/auth")
    # 4. Vault routes — registered only when the vault runtime is on,
    #    so a disabled vault publishes no schema and no endpoints.
    if vault_enabled():
        from app.vault.routes import router as vault_router
        from app.vault.routes import vault_saturation_handler

        # SQLAlchemy is the vault's dependency alone. Keep this handler behind
        # the same gate so disabled startup never imports the vault route stack.
        app.add_exception_handler(SQLAlchemyTimeoutError, vault_saturation_handler)
        app.include_router(vault_router, prefix="/api/v1/vault")

        # The MCP adapter over the same services. Mounted rather than included
        # because it is a whole ASGI application, not a router -- which is also
        # why it carries its own authentication and pre-auth guard: a mount
        # inherits neither the router's dependencies nor the host's exception
        # handlers. See app/vault/mcp.py.
        from app.vault.mcp import build_vault_mcp_app

        # Registered before the mount so the exact path matches here. A mount
        # answers only its trailing-slash form, and the bare form falls through
        # to a bare 405 -- which is precisely the URL an operator types when
        # configuring a client. 307 preserves the method and body, so a POSTed
        # JSON-RPC call survives the hop.
        #
        # It deliberately does no work and touches no credential, so running
        # ahead of the vault's pre-auth guard costs nothing; everything is
        # enforced at the target.
        @app.api_route(
            "/api/v1/vault/mcp",
            methods=["GET", "POST", "DELETE"],
            include_in_schema=False,
        )
        async def vault_mcp_canonical_path() -> RedirectResponse:
            return RedirectResponse("/api/v1/vault/mcp/", status_code=307)

        # Held on app state, not in a module-level cache: the transport's
        # session manager refuses a second run() on the same instance, so each
        # application must own its own.
        app.state.vault_mcp_app = build_vault_mcp_app()
        app.mount("/api/v1/vault/mcp", app.state.vault_mcp_app)

        # 4b. The OAuth authorization server (vault ADR 0024), root-mounted
        #     because RFC 9728 fixes the protected-resource metadata path
        #     relative to the host, and because the SDK builds /authorize and
        #     friends from issuer_url — so both have to agree they live at the
        #     root. Registered here for the same reason every explicit route is:
        #     before the SPA catch-all.
        #
        #     Gated on VAULT_PUBLIC_URL rather than a boolean of its own. These
        #     routes publish discovery metadata containing absolute URLs, so a
        #     deployment that cannot state its own origin cannot serve them
        #     correctly — and ADR 0024 is explicit that advertising an
        #     authorization server before one answers is worse than the honest
        #     dead end of a bare 401. Absence of the variable is therefore the
        #     off switch, and it needs no second flag to forget to set.
        public_url = (os.environ.get("VAULT_PUBLIC_URL") or "").rstrip("/")
        if public_url:
            from app.vault.browse_console import build_vault_browse_routes
            from app.vault.oauth_routes import build_vault_oauth_routes
            from app.vault.review_console import build_vault_review_routes

            app.router.routes.extend(
                build_vault_oauth_routes(
                    issuer_url=public_url,
                    mcp_url=f"{public_url}/api/v1/vault/mcp",
                )
            )
            # Both consoles authorize themselves through the routes above, so
            # they are gated on the same variable: without a reachable issuer
            # they could render but never sign in. See vault ADR 0037 for the
            # reviewer and ADR 0039 for the browser.
            app.router.routes.extend(build_vault_review_routes())
            app.router.routes.extend(build_vault_browse_routes())
    # 5. SPA assets mount — MUST come before the SPA catch-all router below.
    spa_routes.mount_spa_assets(app)
    # 6. SPA catch-all router — registered LAST so the explicit Jinja routes on / and /leaderboard win.
    app.include_router(spa_routes.router)
    # 7. Static files (served at root, so this goes last to avoid shadowing API and view routes)
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
    return app


app = create_app()
