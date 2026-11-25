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
        yield
    finally:
        await close_db()
        await close_cache()


def create_app() -> FastAPI:
    # Loaded before registration so anything read while assembling routes sees
    # .env, as it did when this also decided whether to register the vault.
    # load_environment is cached and uses override=False, so the lifespan's
    # call is a no-op and the process environment still wins over .env.
    load_environment()

    app = FastAPI(title="Leaderboard API", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    app.add_exception_handler(CrossRouteError, _cross_route_handler)

    # Rate limiting here is deliberately per-route: every limited endpoint
    # carries its own `@limiter.limit`, chosen for what that endpoint costs.
    # SlowAPIMiddleware is NOT registered, and that is the decision rather
    # than an omission -- it applies the Limiter's `default_limits`, which is
    # empty, so it read as global rate limiting while enforcing nothing.
    # Adding it back means choosing a global default first; the middleware
    # alone would still cover nothing.
    #
    # `app.state.limiter` and the RateLimitExceeded handler above are still
    # required: the decorators raise through them.

    # CORS is registered outermost (Starlette applies middleware in reverse
    # registration order) so it handles preflight OPTIONS first and so CORS
    # headers are present on 429 and 5xx responses raised further in.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ALLOWED_ORIGINS),
        # Browser CORS is for the public leaderboard/auth surfaces. The vault
        # is a separate application this middleware does not wrap (host ADR
        # 0016), so PUT/DELETE stay absent.
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
    # 4. The vault is a separate application (host ADR 0016). composed.py
    #    routes its paths to it ahead of this app, so nothing is registered here.
    # 5. SPA assets mount — MUST come before the SPA catch-all router below.
    spa_routes.mount_spa_assets(app)
    # 6. SPA catch-all router — registered LAST so the explicit Jinja routes on / and /leaderboard win.
    app.include_router(spa_routes.router)
    # 7. Static files (served at root, so this goes last to avoid shadowing API and view routes)
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
    return app


app = create_app()
