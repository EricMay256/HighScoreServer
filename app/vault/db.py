"""Async SQLAlchemy engine lifecycle for vault persistence."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock
from time import perf_counter

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from .domain import PoolSnapshot
from .settings import VaultSettings


logger = logging.getLogger(__name__)

# The generated column's expression is the only authority on which text search
# configuration the corpus was actually indexed with; the environment variable
# is merely what this process believes.
_SEARCH_VECTOR_EXPRESSION_QUERY = text(
    """
    SELECT pg_get_expr(d.adbin, d.adrelid)
    FROM pg_attribute a
    JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'vault.vault_documents'::regclass
      AND a.attname = 'search_vector'
      AND a.attgenerated = 's'
    """
)


class VaultPoolObserver:
    """Small in-process pool observer with no metrics-backend dependency."""

    def __init__(self, pool_size: int) -> None:
        self._pool_size = pool_size
        self._checked_out = 0
        self._checkout_count = 0
        self._checkin_count = 0
        self._checkout_failures = 0
        self._latest_checkout_seconds: float | None = None
        self._maximum_checkout_seconds: float | None = None
        self._total_checkout_seconds = 0.0
        self._lock = Lock()

    def checked_out(self) -> None:
        with self._lock:
            self._checked_out += 1
            self._checkout_count += 1

    def checked_in(self) -> None:
        with self._lock:
            self._checked_out = max(0, self._checked_out - 1)
            self._checkin_count += 1

    def checkout_completed(self, elapsed_seconds: float) -> None:
        with self._lock:
            self._latest_checkout_seconds = elapsed_seconds
            self._total_checkout_seconds += elapsed_seconds
            if (
                self._maximum_checkout_seconds is None
                or elapsed_seconds > self._maximum_checkout_seconds
            ):
                self._maximum_checkout_seconds = elapsed_seconds

    def checkout_failed(self) -> None:
        with self._lock:
            self._checkout_failures += 1

    def snapshot(self) -> PoolSnapshot:
        with self._lock:
            return PoolSnapshot(
                pool_size=self._pool_size,
                checked_out=self._checked_out,
                checkout_count=self._checkout_count,
                checkin_count=self._checkin_count,
                checkout_failures=self._checkout_failures,
                latest_checkout_seconds=self._latest_checkout_seconds,
                maximum_checkout_seconds=self._maximum_checkout_seconds,
                total_checkout_seconds=self._total_checkout_seconds,
            )


_engine: AsyncEngine | None = None
_observer: VaultPoolObserver | None = None


def create_vault_engine(
    settings: VaultSettings,
) -> tuple[AsyncEngine, VaultPoolObserver]:
    """Create one async engine for the current worker."""

    settings.validate_connection_budget()
    observer = VaultPoolObserver(settings.pool_size)
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "checkout")
    def _checkout(*_args: object) -> None:
        observer.checked_out()

    @event.listens_for(engine.sync_engine, "checkin")
    def _checkin(*_args: object) -> None:
        observer.checked_in()

    return engine, observer


async def assert_text_search_config(engine: AsyncEngine, expected: str) -> None:
    """Fail fast when the migrated ``search_vector`` disagrees with the environment.

    A persisted generated column's expression is compiled into DDL at migration
    time, so no environment change can move it. Without this check a mismatch is
    silent: queries would be parsed with one configuration while the stored
    vectors and their GIN index were built with another.
    """

    async with engine.connect() as connection:
        result = await connection.execute(_SEARCH_VECTOR_EXPRESSION_QUERY)
        expression = result.scalar_one_or_none()

    if expression is None:
        raise RuntimeError(
            "vault.vault_documents.search_vector is not a stored generated "
            "column. The vault schema is absent or out of date — run "
            "'alembic -c alembic-vault.ini upgrade head'."
        )
    if f"'{expected}'::regconfig" not in expression:
        raise RuntimeError(
            f"VAULT_TEXT_SEARCH_CONFIG={expected!r} does not match the migrated "
            f"search_vector expression ({expression}). Changing the text search "
            "configuration requires a table rewrite and a GIN reindex, not a "
            "restart."
        )


async def init_vault_db() -> None:
    """Initialize the worker-local vault engine when explicitly enabled."""

    global _engine, _observer
    settings = VaultSettings.from_environment()
    if not settings.enabled:
        return
    if _engine is not None:
        raise RuntimeError("Vault database engine is already initialized")

    budget = settings.validate_connection_budget()
    engine, observer = create_vault_engine(settings)
    try:
        await assert_text_search_config(engine, settings.text_search_config)
    except Exception:
        await engine.dispose()
        raise
    _engine, _observer = engine, observer
    logger.info(
        "Vault database engine initialized",
        extra={
            "vault_shared_database": budget.shared_database,
            "vault_pool_size": settings.pool_size,
            "hss_pool_max_size": settings.hss_pool_max_size,
            "database_connection_limit": budget.hss_limit,
            "vault_database_connection_limit": budget.vault_limit,
            "combined_allocated_connections": budget.combined_allocated,
        },
    )


def get_vault_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Vault database engine is not initialized")
    return _engine


def get_vault_pool_snapshot() -> PoolSnapshot:
    if _observer is None:
        raise RuntimeError("Vault database engine is not initialized")
    return _observer.snapshot()


@asynccontextmanager
async def acquire_vault_connection(
    engine: AsyncEngine | None = None,
    observer: VaultPoolObserver | None = None,
) -> AsyncIterator[AsyncConnection]:
    """Acquire a measured connection without beginning a transaction."""

    selected_engine = engine or get_vault_engine()
    selected_observer = observer or _observer
    started_at = perf_counter()
    try:
        connection = await selected_engine.connect()
    except Exception:
        if selected_observer is not None:
            selected_observer.checkout_failed()
        raise
    if selected_observer is not None:
        selected_observer.checkout_completed(perf_counter() - started_at)
    try:
        yield connection
    finally:
        await connection.close()


async def close_vault_db() -> None:
    global _engine, _observer
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _observer = None
