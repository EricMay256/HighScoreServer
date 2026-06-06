import os
from psycopg_pool import AsyncConnectionPool


_pool: AsyncConnectionPool | None = None


async def init_db() -> None:
    """Open the async connection pool. Called from main.py lifespan.

    The pool is constructed with open=False and opened explicitly with
    await pool.open() — psycopg_pool deprecates opening in the constructor,
    and the pool must be opened on the running event loop.
    """
    global _pool
    url = os.environ["DATABASE_URL"]

    # Heroku provides postgres://; normalize to postgresql:// for clarity.
    # (psycopg3 accepts both, but keep the invariant explicit.)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    _pool = AsyncConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=10,
        open=False,
    )
    await _pool.open()


def get_pool() -> AsyncConnectionPool:
    """Return the open pool. Use as `async with get_pool().connection() as conn:`.

    The connection context manager wraps a transaction: it commits on a clean
    exit, rolls back on exception, and returns the connection to the pool either
    way — so call sites do not manage commit/rollback/release by hand.
    """
    if _pool is None:
        raise RuntimeError("Connection pool not initialized")
    return _pool


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
