import os
import psycopg2
from psycopg2 import pool


_connection_pool: pool.SimpleConnectionPool | None = None


def init_db() -> None:
    global _connection_pool
    url = os.environ["DATABASE_URL"]

    # Heroku provides postgres:// but psycopg2 requires postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    _connection_pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=url,
    )


def get_conn() -> psycopg2.extensions.connection:
    if _connection_pool is None:
        raise RuntimeError("Connection pool not initialized")
    return _connection_pool.getconn()


def release_conn(conn: psycopg2.extensions.connection) -> None:
    if _connection_pool is None:
        return;
    try:
        conn.rollback()
    except Exception:
        # Connection is broken; allow pool to discard it on putconn.
        pass
    _connection_pool.putconn(conn)


def close_db() -> None:
    if _connection_pool is not None:
        _connection_pool.closeall()