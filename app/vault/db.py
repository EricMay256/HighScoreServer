"""Async SQLAlchemy engine lifecycle for vault persistence."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from threading import Lock
from time import perf_counter

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from .domain import PoolSnapshot
from .settings import VaultSettings, vault_enabled


def describe_database(url: str) -> str:
    """Host, port, and database name only -- never the credential.

    For scripts to print **before** they act, so an operator can see which
    database is about to be written to, exported from, or pruned. Three scripts
    had a private copy of this and the two that most needed it had none:
    ``import_vault_wiki`` and ``prune_vault_oauth`` both resolved a URL silently
    and then wrote.

    That silence has a cost. ``VaultSettings.from_environment`` reads
    ``VAULT_DATABASE_URL`` first and falls back to ``DATABASE_URL``, and
    ``load_environment`` fills either from ``.env`` when the process has none --
    so "which database am I talking to" has three possible answers and none of
    them was visible. A run against the wrong one fails confusingly at best, and
    at worst succeeds.

    Renders ``host[:port]/database``. The port appears only when the URL states
    one, since inventing the default would claim something the URL did not say.
    Every part that can be absent has a word for being absent -- an operator
    reading this is deciding whether to proceed, and ``None`` or an empty space
    where a database name belongs is exactly the ambiguity this exists to
    remove.

    IPv6 hosts are bracketed. ``::1:5432/db`` leaves a reader guessing where the
    address stops and the port starts; ``[::1]:5432/db`` does not.

    Never the password: this string is printed to a terminal and pasted into
    issues.
    """

    parsed = make_url(url)
    host = parsed.host or "(local socket)"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.database or "(no database)"
    return f"{host}{port}/{database}"


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
        self._maximum_checked_out = 0
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
            # The high-water mark is taken here, inside the lock, because it
            # cannot be recovered afterwards. ``checked_out`` is an
            # instantaneous gauge: sampling it reports whatever the pool held at
            # the moment somebody looked, and a peak that lasted 40ms under load
            # is invisible to any polling interval. A running maximum is
            # sampling-immune -- read it whenever you like and it still answers
            # "what is the most this worker ever held at once", which is the
            # question the connection budget is actually about.
            self._maximum_checked_out = max(
                self._maximum_checked_out, self._checked_out
            )

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
                maximum_checked_out=self._maximum_checked_out,
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
    # The feature gate is the only VAULT_* setting that may be parsed while the
    # package is disabled. This keeps dormant, malformed configuration inert as
    # promised by the host deployment contract.
    if not vault_enabled():
        return
    settings = VaultSettings.from_environment()
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


# Default cadence for the periodic pool line. The maxima it reports are
# cumulative, so the interval decides how much *trend* you get, not whether the
# peak is captured -- a peak that happened at any point still appears in every
# later line and in the final one. Five minutes is therefore a readability
# choice rather than a fidelity one. Set 0 to log only at shutdown.
_DEFAULT_POOL_LOG_INTERVAL_SECONDS = 300.0


def log_vault_pool_snapshot(reason: str) -> None:
    """Emit one pool line, at WARNING when the pool has actually refused work.

    ``checkout_failures`` is the number that decides whether the pool is big
    enough: it counts requests that waited out ``pool_timeout`` and were
    refused, which is the path the saturation handler turns into a 503. A
    deployment with zero of those has headroom regardless of how busy it looked;
    one with any has already failed a caller. So the level is chosen by that
    field rather than fixed, and a log search for the warning finds every
    occurrence without reading the numbers.

    Per worker, necessarily -- the observer counts one process's checkouts and
    knows nothing of its siblings. The connection budget is expressed the same
    way (per-worker pool size times worker count), so these line up; what they
    cannot see is the database-wide total, which includes the release dyno and
    any operator session. Pair with pg_stat_activity for that.
    """

    if _observer is None:
        return
    snapshot = _observer.snapshot()
    level = logging.WARNING if snapshot.checkout_failures else logging.INFO
    logger.log(
        level,
        "Vault pool %s: peak %d/%d concurrent, %d failures",
        reason,
        snapshot.maximum_checked_out,
        snapshot.pool_size,
        snapshot.checkout_failures,
        extra={
            "vault_pool_reason": reason,
            "vault_pool_size": snapshot.pool_size,
            "vault_pool_checked_out": snapshot.checked_out,
            "vault_pool_maximum_checked_out": snapshot.maximum_checked_out,
            "vault_pool_checkout_count": snapshot.checkout_count,
            "vault_pool_checkout_failures": snapshot.checkout_failures,
            "vault_pool_maximum_wait_seconds": snapshot.maximum_checkout_seconds,
        },
    )


@asynccontextmanager
async def report_vault_pool(
    interval_seconds: float | None = None,
) -> AsyncIterator[None]:
    """Log the pool periodically, and once more on the way out.

    The closing line is the one that matters and is why this is a context
    manager rather than a bare task: it runs on shutdown, after the worker has
    served whatever it was going to serve, so it carries the final high-water
    mark for that process. A deploy or a dyno restart therefore leaves the
    complete answer in the log even if nobody was watching.

    Failures here must never take the application down -- this is
    instrumentation, and a broken log line is not worth a failed boot.
    """

    if interval_seconds is None:
        interval_seconds = float(
            os.environ.get(
                "VAULT_POOL_LOG_INTERVAL_SECONDS",
                _DEFAULT_POOL_LOG_INTERVAL_SECONDS,
            )
        )

    async def report_periodically() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            log_vault_pool_snapshot("interval")

    task = (
        asyncio.create_task(report_periodically()) if interval_seconds > 0 else None
    )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            # Suppressed rather than awaited bare: cancellation is the expected
            # end of this task, not an error to surface during shutdown.
            with suppress(asyncio.CancelledError):
                await task
        log_vault_pool_snapshot("final")


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
