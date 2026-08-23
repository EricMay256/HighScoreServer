"""The pool observer's high-water mark and its reporting.

These are pure unit tests on the counters -- no database, no engine. What they
protect is the one property the connection-budget review depends on: that the
peak number of simultaneous checkouts is *recorded when it happens* rather than
sampled afterwards. Every other field in the snapshot can be read late and still
be true; this one cannot.
"""

import asyncio
import logging

import pytest

from app.vault import db as vault_db
from app.vault.db import (
    VaultPoolObserver,
    log_vault_pool_snapshot,
    report_vault_pool,
)


# The logger every test here names in `at_level`. `caplog.records` is every
# record the run produced rather than only that logger's, so an unrelated
# `psycopg_pool` worker error from another test lands in these assertions --
# which is how the search suite's no-error test failed once carrying three
# pool-worker errors it had nothing to do with.
_LOGGER = "app.vault.db"


def _levels(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [record.levelno for record in caplog.records if record.name == _LOGGER]


def test_high_water_mark_records_the_peak_not_the_current_depth() -> None:
    """The number the budget review needs is the maximum, not the instant.

    Three simultaneous checkouts followed by three checkins leaves the gauge at
    zero. Sampling it then -- which is all an endpoint or a periodic poll can do
    -- reports a pool that was never busy. The running maximum is what
    distinguishes "held three at once" from "held one, three times".
    """

    observer = VaultPoolObserver(pool_size=2)

    for _ in range(3):
        observer.checked_out()
    for _ in range(3):
        observer.checked_in()

    snapshot = observer.snapshot()
    assert snapshot.checked_out == 0
    assert snapshot.maximum_checked_out == 3
    assert snapshot.checkout_count == 3


def test_sequential_use_never_raises_the_high_water_mark() -> None:
    """Volume is not concurrency, and the pool only cares about the second.

    A hundred checkouts that never overlap peak at one. If this ever reported
    more, the metric would be counting throughput and would say a busy quiet
    service was saturated.
    """

    observer = VaultPoolObserver(pool_size=2)

    for _ in range(100):
        observer.checked_out()
        observer.checked_in()

    assert observer.snapshot().maximum_checked_out == 1


def test_high_water_mark_survives_a_later_quiet_period() -> None:
    observer = VaultPoolObserver(pool_size=2)

    observer.checked_out()
    observer.checked_out()
    observer.checked_in()
    observer.checked_in()
    # A long quiet spell afterwards must not erode the evidence.
    for _ in range(10):
        observer.checked_out()
        observer.checked_in()

    assert observer.snapshot().maximum_checked_out == 2


def test_checkin_does_not_drive_the_gauge_negative() -> None:
    """An unmatched checkin must not corrupt later peaks.

    SQLAlchemy emits checkin for connections this observer may not have seen
    checked out -- during dispose, for instance. Clamping at zero keeps a
    subsequent genuine peak accurate instead of offset.
    """

    observer = VaultPoolObserver(pool_size=2)

    observer.checked_in()
    observer.checked_in()
    observer.checked_out()

    snapshot = observer.snapshot()
    assert snapshot.checked_out == 1
    assert snapshot.maximum_checked_out == 1


def test_pool_line_is_a_warning_once_the_pool_has_refused_work(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """checkout_failures is the field that decides whether the pool is big enough.

    A refused checkout is a caller that received a 503, so it is escalated to
    WARNING and can be found by level alone rather than by reading numbers.
    """

    observer = VaultPoolObserver(pool_size=2)
    observer.checkout_failed()
    monkeypatch.setattr(vault_db, "_observer", observer)

    with caplog.at_level(logging.INFO, logger="app.vault.db"):
        log_vault_pool_snapshot("test")

    assert _levels(caplog) == [logging.WARNING]


def test_pool_line_is_informational_while_nothing_has_been_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observer = VaultPoolObserver(pool_size=2)
    observer.checked_out()
    monkeypatch.setattr(vault_db, "_observer", observer)

    with caplog.at_level(logging.INFO, logger="app.vault.db"):
        log_vault_pool_snapshot("test")

    assert _levels(caplog) == [logging.INFO]


def test_pool_line_is_skipped_when_no_engine_was_initialized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Instrumentation must not raise where the vault is switched off."""

    monkeypatch.setattr(vault_db, "_observer", None)

    with caplog.at_level(logging.INFO, logger="app.vault.db"):
        log_vault_pool_snapshot("test")

    assert _levels(caplog) == []


def test_reporter_emits_a_final_line_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The closing line is the point: it carries the worker's final peak.

    A deploy or dyno restart must leave the complete answer in the log even when
    nobody was watching, so this runs on the way out rather than on a timer.
    """

    observer = VaultPoolObserver(pool_size=2)
    observer.checked_out()
    observer.checked_out()
    monkeypatch.setattr(vault_db, "_observer", observer)

    async def exercise() -> None:
        # interval 0 disables the periodic task, leaving only the exit line.
        async with report_vault_pool(interval_seconds=0):
            pass

    with caplog.at_level(logging.INFO, logger="app.vault.db"):
        asyncio.run(exercise())

    finals = [
        r
        for r in caplog.records
        if r.name == _LOGGER
        and getattr(r, "vault_pool_reason", None) == "final"
    ]
    assert len(finals) == 1
    assert finals[0].vault_pool_maximum_checked_out == 2


def test_reporter_cancels_its_task_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not surface the reporter's own cancellation as an error."""

    monkeypatch.setattr(vault_db, "_observer", VaultPoolObserver(pool_size=2))

    async def exercise() -> None:
        async with report_vault_pool(interval_seconds=3600):
            await asyncio.sleep(0)

    asyncio.run(exercise())
