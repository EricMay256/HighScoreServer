# CLAUDE.md — HighScoreServer

Standing context for Claude Code working in this repository. Read this fully before any task.
If a `CLAUDE.md` already exists in the repo, **merge** with it rather than clobbering — reconcile conflicts in favor of the live file and flag them.

## What this project is

HighScoreServer (HSS): a leaderboard backend. FastAPI + PostgreSQL (raw SQL via psycopg2), deployed on Heroku (`high-score-server`). It serves a Unity C# client, a vanilla-JS Jinja2 web view, and a React SPA (`leaderboard-frontend/`, mounted at `/app`; Jinja2 views remain at `/`). Companion game: Flick Fest.

Dual purpose: a working game backend **and** a portfolio piece that must be defensible in an interview. Prefer idiomatic over clever; document tradeoffs; every architectural decision should be explainable out loud.

## Stack (settled — do not re-litigate)

- **API:** FastAPI, synchronous.
- **DB:** PostgreSQL via psycopg2, **raw SQL, no ORM**. Window functions, upserts, and dynamic `ORDER BY` are deliberately hand-written.
- **Auth:** JWT (HS256) access tokens + opaque refresh tokens (SHA-256 hashed, rotated on refresh via `DELETE ... RETURNING`). Guest accounts created silently via `POST /api/auth/guest`; upgraded in place by `/claim`.
- **Cache:** pluggable backend — in-process `cachetools` by default, Redis opt-in via `CACHE_BACKEND`. 120s TTL, graceful fallback when the cache is unavailable.
- **Rate limiting:** slowapi.
- **Migrations:** Alembic, **raw-SQL migrations** (`op.execute` with hand-written DDL — no SQLAlchemy models, no autogenerate). See `specs.md` Phase 0. Before Phase 0, schema lived in `db/schema.sql` as `CREATE TABLE IF NOT EXISTS`.
- **Hosting:** Heroku. Prefer existing add-ons (Postgres, Redis). **Do not introduce new infrastructure** without explicit approval.

Stack decisions (Python vs Node, FastAPI vs Flask, sync vs async, raw SQL vs ORM) are settled. Do not propose changing them unless explicitly asked.

## Repo orientation

- `app/` — FastAPI app. Routes split by concern (`leaderboard_routes.py`, auth routes, `view_routes.py`). `models.py` holds Pydantic models. `periods.py` defines `PERIODS` and `get_period_start`. Connection-pool, cache, and limiter helpers live in their own modules.
- `db/` — `schema.sql`, `role.sql` (grants for the `leaderboard_app` role), `seed.sql`.
- `docs/adr/` — Architecture Decision Records, Nygard format (strict superseding from 0008 onward).
- `scripts/` — operational scripts (`prune_guests.py`, `prune_refresh_tokens.py`).
- `tests/` — pytest suite.
- `templates/` — Jinja2 web view.
- `leaderboard-frontend/` — Vite + TypeScript + TanStack Query React SPA.
- Unity client and Flick Fest are C# (UnityWebRequest + Newtonsoft.Json), in their own locations.

## Code style — Python

- Type hints on **every** function signature.
- Pydantic models for anything crossing an API boundary.
- Explicit error handling with `HTTPException`. Never a bare `except` that swallows. Catch specific psycopg2 errors (e.g. `ForeignKeyViolation`, `UniqueViolation`/`23505`) and map them to clean HTTP statuses.
- Clarity over cleverness — this code is read in interviews.
- **No module-level side effects.** `sys.exit`, `load_environment()`, `logging.basicConfig()` at import scope prevent safe import — guard under `if __name__ == "__main__"`.
- `sort_order` / `period` are interpolated into SQL **only** from DB-sourced or validated-literal values, never raw user input. Preserve that invariant.

## Code style — C#

- `UnityWebRequest` for HTTP, `Newtonsoft.Json` for serialization, coroutines for async Unity work.
- **Any change to an API response shape must update the C# models in the same change.** Enums use `StringEnumConverter` / `EnumMember`.

## Key invariants (don't break silently)

- `scores` is **upsert-on-best** per `UNIQUE (user_id, game_mode, period, period_start)`. Best-wins respects the mode's `sort_order` via the improvement predicate. (This feature adds a **cumulative** strategy — see `specs.md`.)
- **Period bucketing:** each submission writes one row per period in `PERIODS`, keyed by `period_start`.
- Guests are real `users` rows (`is_guest=true`); `scores.user_id` is `ON DELETE RESTRICT` — never orphan leaderboard history. Guest pruning checks for score ownership first.
- `game_modes.name` is a natural key (small, stable, human-readable in logs/SQL). The guest-gating column is **`requires_claimed_account`** (blocks guests when true).

## Working agreements

- **Phased work** with an explicit handoff document at each session boundary. Build incremental stability; don't attempt the whole feature in one pass.
- **Section-by-section diffs over full rewrites.** Minimal changes unless a rewrite is clearly warranted. Match the existing module structure rather than proposing new layouts.
- When a decision has two reasonable answers, **enumerate the tradeoffs and surface the fork** — don't resolve it silently.
- **Flag anything that requires a migration** (now: an Alembic revision) rather than just a code change.
- **Flag any new dependency** with the install command and a reminder to update the lockfile / `pip freeze`.
- **Flag the async/sync boundary explicitly** — it's the likeliest source of subtle bugs here.
- Note whether an approach is **idiomatic or a pragmatic shortcut**.
- **Flag uncertainty** on library/version specifics rather than presenting a guess as fact.
- **ADR discipline:** material architectural decisions get a Nygard-format ADR. Propose ADR stubs when you make such a decision.

## How to run

- Tests: `pytest`
- Dev server: `uvicorn app.main:app --reload` (interactive docs at `/docs`)
- DB schema is managed by **Alembic** (raw-SQL migrations; baseline `0001_baseline` reflects the pre-Alembic schema). `db/schema.sql` is a labeled bootstrap snapshot, not the source of truth.
  - Fresh/empty DB (CI, new clone): `alembic upgrade head` builds the whole schema.
  - Existing DB that predates Alembic: `alembic stamp 0001_baseline` once (never `upgrade` — the objects already exist), then `upgrade head` for later revisions.
  - `env.py` reads `DATABASE_URL`, loading `.env` with `override=False` — so a process-env URL (a throwaway DB, or prod) overrides `.env`. Always `echo $env:DATABASE_URL` before a stamp/upgrade against a non-default target.
- Deploy (Heroku): migrations run automatically via the Procfile `release: alembic upgrade head` phase; a failed migration aborts the release. No manual migration step on deploy.
- DB ad-hoc, Heroku: `cat file.sql | heroku pg:psql --app high-score-server`, or `heroku run alembic current --app high-score-server`.
- **Windows / PowerShell** is the primary dev environment:
  - `curl` is an alias for `Invoke-WebRequest` (throws on non-2xx) — use `curl.exe`.
  - Stdin redirection (`< file`) is unsupported — use `Get-Content file | heroku pg:psql`.
  - Env vars set with `$env:` are per-process and do not persist across shells; `.env` is loaded by the app and by Alembic's `env.py`, not by the shell.

## Scope guardrails for current work

See `specs.md` for the active feature spec. Deferred / out of scope unless explicitly raised: asyncpg / async migration; server-issued seeds; normalized per-action tables (blob is used instead); admin review UI; password reset; React integration of runs.
