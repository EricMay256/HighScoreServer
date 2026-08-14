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
from starlette.responses import JSONResponse

from app import spa_routes
from app.auth_routes import router as auth_router
from app.cache import close_cache, init_cache
from app.db import close_db, init_db
from app.env import load_environment, validate_environment
from app.leaderboard_routes import CrossRouteError
from app.leaderboard_routes import router as leaderboard_router
from app.limiter import limiter
from app.vault.db import close_vault_db, init_vault_db
from app.vault.embedding_runtime import close_vault_embeddings, init_vault_embeddings
from app.vault.routes import router as vault_router
from app.vault.routes import vault_saturation_handler
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

    await init_db()
    try:
        init_cache()
        await init_vault_db()
        if vault_enabled():
            # Only after the engine is up: a provider with no corpus to search
            # is not a useful thing to have running.
            await init_vault_embeddings()
        yield
    finally:
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
    # Vault pool saturation is a 503, not a 500. SQLAlchemy is the vault's
    # dependency alone, so this cannot mask an HSS failure, and a disabled
    # vault has no engine to raise it -- hence no need to gate registration.
    app.add_exception_handler(SQLAlchemyTimeoutError, vault_saturation_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Register CORS after SlowAPI (Starlette uses reverse registration order)
    # so it runs first on requests and handles preflight OPTIONS before any
    # global rate-limit accounting. Per-route limits are applied at the
    # decorator, but ordering CORS outermost also ensures CORS headers are
    # present on 429 and 5xx responses from inner middleware.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ALLOWED_ORIGINS),
        # OPTIONS not required but included for clarity
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
    # 4. Vault read-only routes — registered only when the vault runtime is on,
    #    so a disabled vault publishes no schema and no endpoints.
    if vault_enabled():
        app.include_router(vault_router, prefix="/api/v1/vault")
    # 5. SPA assets mount — MUST come before the SPA catch-all router below.
    spa_routes.mount_spa_assets(app)
    # 6. SPA catch-all router — registered LAST so the explicit Jinja routes on / and /leaderboard win.
    app.include_router(spa_routes.router)
    # 7. Static files (served at root, so this goes last to avoid shadowing API and view routes)
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
    return app


app = create_app()
