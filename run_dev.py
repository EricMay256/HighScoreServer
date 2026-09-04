"""Local development server launcher (Windows-safe async).

Why this file exists
--------------------
psycopg3's async pool cannot run on Windows' default ``ProactorEventLoop`` — it
drives sockets with ``loop.add_reader``/``add_writer``, which only
``SelectorEventLoop`` implements. uvicorn creates its event loop *before* it
imports the app, so the policy cannot be set from inside the ``app`` package
(by then the Proactor loop already exists). It must be set here, ahead of
``uvicorn.run``.

The policy is set at *module scope* (not under ``if __name__ == "__main__"``)
on purpose: uvicorn's ``--reload`` worker is a spawned subprocess that
re-imports this module before starting its own loop, so module-scope setup is
re-applied there too.

On Linux/macOS this is a no-op (SelectorEventLoop is already the default), and
production does not use this launcher at all — Heroku runs
``gunicorn ... -k uvicorn.workers.UvicornWorker`` on Linux. So this is purely a
local-Windows-dev convenience.

Run:
    python run_dev.py
"""
import asyncio
import os
import sys


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("DEV_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEV_PORT", "8000")),
        reload=True,
    )
