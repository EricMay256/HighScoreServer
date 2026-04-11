# app/spa_routes.py
from pathlib import Path
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Resolve the SPA build directory relative to THIS file, not the CWD.
# Without this, `uvicorn app.main:app` works from the repo root but breaks
# the moment anyone runs it from a different directory (e.g. a systemd
# unit, a Docker WORKDIR, or a test runner). Path(__file__) anchors us
# to the module location regardless of how the process was launched.
_SPA_DIST = Path(__file__).resolve().parent.parent / "leaderboard-frontend" / "dist"
_SPA_INDEX = _SPA_DIST / "index.html"

router = APIRouter()


@router.get("/app")
@router.get("/app/{full_path:path}")
async def spa_index(full_path: str = "") -> FileResponse:
    """
    Serve the React SPA's index.html for /app and any sub-path.

    The catch-all exists so that if you later add client-side routing
    (react-router, TanStack Router, etc.), deep links like /app/profile
    still return index.html and let the client router take over.

    Note: this route does NOT serve assets — those are handled by the
    StaticFiles mount registered in main.py. Mount order matters; see
    the comment there.
    """
    if not _SPA_INDEX.is_file():
        # Build artifact missing — surface a clear error rather than a
        # confusing 500 from FileResponse. Most likely cause: forgot to
        # run `npm run build` in leaderboard-frontend/.
        raise HTTPException(
            status_code=503,
            detail="SPA build not found. Run `npm run build` in leaderboard-frontend/.",
        )
    return FileResponse(_SPA_INDEX)


def mount_spa_assets(app: FastAPI) -> None:
    """Mount the hashed asset directory. Called from main.py."""
    assets_dir = _SPA_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/app/assets", StaticFiles(directory=assets_dir), name="spa_assets")
    # If the dir doesn't exist yet (fresh clone, no build), we silently
    # skip the mount. spa_index will return 503 with a helpful message
    # when someone hits /app, which is a better failure mode than
    # crashing on startup.