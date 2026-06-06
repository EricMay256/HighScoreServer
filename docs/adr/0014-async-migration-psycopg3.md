# 14. Async migration to psycopg3

Date: 2026-06-06

## Status

Accepted

Supersedes [0005](0005-sync-over-async.md).

## Context

ADR 0005 chose synchronous `def` handlers backed by psycopg2, with FastAPI
running handlers in a threadpool. It named the trigger for revisiting —
threadpool queue depth, p95 latency under load, `H12` timeouts — and the
migration path: psycopg2 → asyncpg with raw SQL preserved, deferred until a
concrete concurrency problem justified the cost.

This ADR records that migration being executed. The driver chosen is **psycopg3
(`psycopg`) in async mode**, not asyncpg as 0005 anticipated. The reasons:

- **Raw SQL is preserved with the least churn.** psycopg3 keeps the
  `%s`-placeholder paramstyle, tuple rows, and `sqlstate`-named error classes
  psycopg2 used. asyncpg uses `$1` positional placeholders and a different error
  taxonomy, which would have rewritten every query string and every error
  branch on top of the async conversion.
- **One driver, both modes.** psycopg3 exposes a sync API (`psycopg.connect`)
  *and* an async API (`AsyncConnectionPool`, `await cur.execute`). The request
  path uses async; the batch scripts (`scripts/prune_*`) and Alembic migrations
  stay synchronous on the same library. asyncpg would have required either a
  second driver for the sync paths or rewriting them async for no benefit.
- **SQLAlchemy already supports it.** Alembic's `env.py` moves from
  `postgresql+psycopg2://` to `postgresql+psycopg://` — a dialect string change,
  no new dependency.

The migration is a coordinated single pass, as 0005 promised it must be: a
partial conversion (one blocking call left inside an `async def` handler) is
strictly worse than the sync baseline, because it stalls the event loop for
every concurrent request on the worker rather than just one threadpool slot.

## Decision

Use `async def` handlers throughout, backed by psycopg3's `AsyncConnectionPool`.

- **Connections** are acquired with `async with get_pool().connection() as conn`.
  The connection context manager owns the transaction: commit on clean exit,
  rollback on exception, return to pool either way. Manual
  `commit`/`rollback`/`release` is gone except where a transaction must be
  committed *before* raising (the rejected-run path in `/runs` persists the
  rejection with an explicit `await conn.commit()`, then raises 422).
- **Every blocking call that would now run on the event loop is addressed:**
  - **bcrypt** (`hash_password`/`verify_password`) is CPU-bound by design and
    has no async variant. It is offloaded with `await asyncio.to_thread(...)`;
    bcrypt's C implementation releases the GIL, so it runs on another core. This
    preserves the threadpool behavior the sync model had.
  - **Redis cache** uses `redis.asyncio` (already part of the installed `redis`
    package) so its network round-trips await rather than block. The in-memory
    `cachetools` backend's methods are async only for interface parity; their
    bodies are trivial dict operations and the prior `threading.Lock` is removed
    (access is now single-threaded on the event loop).
- **Batch and migration paths stay synchronous** on psycopg3's sync API. They
  are one-shot jobs with no concurrency to gain from async.

## Consequences

- Concurrency for DB-bound work is now bounded by the connection pool size
  (`max_size=10`) and the event loop, not the threadpool. This is the lever to
  tune under load, and it is explicit.
- psycopg2 is removed entirely; `psycopg[binary]` and `psycopg-pool` replace it.
  One Postgres driver in the tree, used sync or async per call site.
- **Known residual sync I/O, deliberately not converted:** slowapi's rate-limit
  storage calls are synchronous. With the default in-memory storage this is pure
  CPU (no concern); with Redis storage it is a sub-millisecond round-trip that
  technically blocks the loop. slowapi 0.1.9 does not cleanly expose async
  storage, and converting would mean reimplementing its middleware against
  `limits.aio` — disproportionate to a sub-ms call next to the ~100ms bcrypt and
  the DB work already moved off the loop. Revisit if rate-limit latency is ever
  observed to matter. `gzip.compress` of the run action log is likewise small
  and left on the loop.
- **Windows local dev requires the SelectorEventLoop policy.** psycopg3's async
  pool drives sockets with `loop.add_reader`/`add_writer`, which Windows'
  default `ProactorEventLoop` does not implement — only `SelectorEventLoop`
  does. Production is unaffected (Heroku is Linux, where Selector is default),
  but Windows (the primary dev environment) needs the policy set *before* the
  loop is created. uvicorn builds its loop before importing the app, so the
  policy can't live in the `app` package; it is set in `run_dev.py` (the dev
  launcher, at module scope so `--reload`'s spawn subprocess re-applies it) and
  in `conftest.py` for the test suite. Python 3.14 deprecates the policy API
  (removal 3.16) but it remains the only lever that reaches uvicorn's and the
  TestClient's loop creation; the deprecation warnings are filtered.
- API response shapes are unchanged, so the Unity C# client needs no update.
- The async/sync boundary is now the primary place a future change can
  reintroduce loop-blocking: any new blocking library call inside a handler must
  be wrapped (`asyncio.to_thread`) or use an async client. This is the footgun
  0005 traded threadpool simplicity to avoid; the discipline now lives in code
  review and this ADR.
