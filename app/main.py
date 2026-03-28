from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.db import init_db, close_db
from app.cache import init_cache, close_cache
from app.env import load_environment, validate_environment
from app.api import router as api_router
from app.views import router as view_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()
    validate_environment()
    init_db()
    init_cache()
    yield
    close_db()
    close_cache()


def create_app() -> FastAPI:
    app = FastAPI(title="Leaderboard API", lifespan=lifespan)
    # 1. API routes
    app.include_router(api_router, prefix="/api")
    # 2. View (Jinja2) routes — no prefix
    app.include_router(view_router)
    # 4. Static files (served at root, so this goes last to avoid shadowing API and view routes)
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
    return app


app = create_app()