"""Isolated launcher for the vault MCP app. **You probably do not need this.**

The MCP server is not a separate process and has nothing to start. It is mounted
into the host application at `/api/v1/vault/mcp/`, behind the same
`VAULT_ENABLED` gate as the vault's HTTP routes, so it is already running
wherever the host runs:

- locally, under `python run_dev.py`, at http://127.0.0.1:8000/api/v1/vault/mcp/
- in production, under gunicorn on Heroku, at the deployed host

Adding it to a client is a URL registration, not a launch — `claude mcp add
--transport http` stores a URL and a header and spawns nothing. So for ordinary
use, run `run_dev.py` (or just deploy) and point the client at the mounted path.

What this file is for
---------------------
One narrow job: serving the MCP app *alone*, on its own port, with no
leaderboard routes, no SPA, and no static mount. That isolation is worth having
when a transport-level failure could plausibly belong either to the adapter or
to the host's routing and middleware — a failure reproduced here cannot be the
host's. It is how the uvicorn event-loop interaction noted at the bottom of this
file was found.

It is not a deployment target. There is deliberately no Procfile entry, and
adding one would put a second unrelated process on the dyno.

Run:
    python run_mcp.py

Then point a client at http://127.0.0.1:8010/mcp with a vault credential. Which
tools appear depends on that credential's scopes — see vault ADR 0021.
"""

import asyncio
import sys


# Local-Windows-dev only, and a no-op everywhere else. psycopg3's async pool
# drives sockets with loop.add_reader/add_writer, which only SelectorEventLoop
# implements, and uvicorn builds its loop before importing the app -- so the
# policy has to be set in the launcher. Same guard as run_dev.py, scripts/, and
# tests/conftest.py; the host AGENTS.md asks that it not be removed as dead code
# when working from WSL. Nothing on the deployed path depends on it.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


MCP_PATH = "/mcp"


def build_standalone_app():
    """Serve the vault MCP application with the vault lifecycle around it.

    The MCP app is mounted inside a thin outer application rather than served
    directly, because the vault engine and embedding provider have to be up
    before any tool runs and the SDK's app knows nothing about either. That
    reintroduces the mount problem ADR 0021 records — a mounted application's
    lifespan does not run — so the outer lifespan enters ``vault_mcp_lifespan``
    explicitly, exactly as ``app.main`` does. Two callers, one reason.
    """

    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.routing import Mount

    from app.vault.db import close_vault_db, init_vault_db
    from app.vault.embedding_runtime import (
        close_vault_embeddings,
        init_vault_embeddings,
    )
    from app.vault.mcp import build_vault_mcp_app, vault_mcp_lifespan

    mcp_app = build_vault_mcp_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await init_vault_db()
        # Only after the engine is up, matching app.main: a provider with no
        # corpus to search is not a useful thing to have running.
        await init_vault_embeddings()
        try:
            async with vault_mcp_lifespan(mcp_app):
                yield
        finally:
            await close_vault_embeddings()
            await close_vault_db()

    return Starlette(lifespan=lifespan, routes=[Mount("/", app=mcp_app)])


if __name__ == "__main__":
    import os

    import uvicorn

    from app.env import load_environment

    # Loaded here rather than at import, per the host AGENTS.md rule against
    # module-level side effects. VAULT_ENABLED has to be visible before the
    # vault modules are imported by the factory below.
    load_environment()

    from app.vault.settings import vault_enabled

    if not vault_enabled():
        # The host application answers a disabled vault by registering no
        # routes at all. A launcher whose only purpose is the vault has no such
        # fallback, so it says why instead of serving an endpoint that would
        # fail on its first tool call.
        sys.exit(
            "VAULT_ENABLED is not true, so there is no vault to serve. "
            "Set VAULT_ENABLED=true in .env or the environment."
        )

    host = os.environ.get("VAULT_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("VAULT_MCP_PORT", "8010"))

    # No --reload. The reload worker is a spawned subprocess that would rebuild
    # the application, and the Streamable HTTP session manager refuses a second
    # run() — the same constraint that keeps the app off a module-level cache
    # in app.main. A restart is a restart.
    print(f"Vault MCP server on http://{host}:{port}{MCP_PATH}")
    # Driven through Config/Server under our own asyncio.run rather than
    # uvicorn.run, because uvicorn.run performs its own event-loop setup
    # in process and overrides the policy set at module scope, which on
    # Windows puts psycopg back on a ProactorEventLoop it cannot use.
    # run_dev.py gets away with uvicorn.run only because reload=True spawns
    # a subprocess that re-imports the module and re-applies the policy.
    # asyncio.run honours the policy already in effect, so the loop is
    # correct before uvicorn ever sees it. A no-op difference on Linux.
    server = uvicorn.Server(
        uvicorn.Config(build_standalone_app(), host=host, port=port)
    )
    asyncio.run(server.serve())
