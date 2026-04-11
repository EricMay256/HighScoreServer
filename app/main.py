from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.db import init_db, close_db
from app.cache import init_cache, close_cache
from app.env import load_environment, validate_environment
from app.leaderboard_routes import router as leaderboard_router
from app.view_routes import router as view_router
from app.auth_routes import router as auth_router
from app.limiter import limiter
from app import spa_routes
import os
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import JSONResponse
from slowapi.middleware import SlowAPIMiddleware

import logging
logger = logging.getLogger(__name__)

async def _custom_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
        headers=dict(exc.headers) if exc.headers else {},
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

    init_db()
    init_cache()
    yield
    close_db()
    close_cache()


def create_app() -> FastAPI:
    app = FastAPI(title="Leaderboard API", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    # 1. View (Jinja2) routes — no prefix
    app.include_router(view_router)
    # 2. Leaderboard routes
    app.include_router(leaderboard_router, prefix="/api/leaderboard")
    # 3. Authentication routes
    app.include_router(auth_router, prefix="/api/auth")
    # 4. SPA assets mount — MUST come before the SPA catch-all router below.
    spa_routes.mount_spa_assets(app)
    # 5. SPA catch-all router — registered LAST so the explicit Jinja routes on / and /leaderboard win.
    app.include_router(spa_routes.router)
    # 6. Static files (served at root, so this goes last to avoid shadowing API and view routes)
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
    return app


app = create_app()