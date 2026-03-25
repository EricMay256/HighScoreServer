from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.db import init_db, close_db
from app.cache import init_cache, close_cache
from app.env import load_environment, validate_environment
from app.routes import router


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
    app.include_router(router, prefix="/api")
    app.mount("/", StaticFiles(directory="public", html=True), name="static")
    return app


app = create_app()