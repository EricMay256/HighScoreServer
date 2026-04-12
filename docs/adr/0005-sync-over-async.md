# 0005. Sync over async

Date: 2026-04-12

## Status

Accepted

## Context

FastAPI supports both `async def` and `def` route handlers. The framework's reputation is built on async, and the default assumption from a reader skimming the codebase is that a FastAPI app should be async throughout. The actual question is whether async is honest given the I/O the application performs.

The database driver is psycopg2, which is synchronous. Calling `cursor.execute()` blocks the calling thread for the duration of the query. FastAPI handles this correctly for `def` handlers by running them in a threadpool, so blocking calls do not stall the event loop. Inside an `async def` handler, however, a blocking call stalls the event loop directly — every other in-flight request on the same worker is paused until the query returns. Wrapping psycopg2 calls in `asyncio.to_thread` or `run_in_executor` would restore correctness, but at that point the handler is async in name only and incurs the threadpool overhead with none of the benefits.

The honest options are: keep psycopg2 and use `def` handlers, or migrate to asyncpg and use `async def` handlers throughout. Mixing them produces the worst of both worlds.

## Decision

Use `def` handlers everywhere. Keep psycopg2. Do not migrate to asyncpg until there is a concrete concurrency problem that justifies the migration cost.

## Consequences

The route signatures honestly reflect what the handlers do: they block on the database, FastAPI runs them in a threadpool, and the threadpool's size is the effective concurrency limit for database-bound work. This is a known and tunable quantity, not a hidden footgun.

A reader expecting async-everywhere will find this surprising and may read it as a mistake. The README's architecture decisions section (now this ADR) exists in part to head off that misreading. For portfolio purposes, being able to articulate *why* a FastAPI app is sync is more valuable than the marginal performance of async would be at current scale — the question itself is a good interview signal.

The migration path, when concurrency becomes a bottleneck, is psycopg2 → asyncpg with raw SQL preserved. SQLAlchemy and Alembic are not on that path (see ADR 0002). The migration is deferred deliberately rather than forgotten: it is a coordinated single-pass change touching every query call site, and doing it speculatively before the bottleneck exists would burn the budget for it without solving a real problem.

The trigger for revisiting this decision is observable: requests piling up in the threadpool queue, dyno-level p95 latency rising under load, or `H12` request-timeout errors in Heroku's router logs. None of those are present today.
