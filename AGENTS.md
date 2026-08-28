# AGENTS.md — HighScoreServer

Standing context for an agent working in this repository. Read this fully before any task.
If a `AGENTS.md` already exists in the repo, **merge** with it rather than clobbering — reconcile conflicts in favor of the live file and flag them.

## What this project is

HighScoreServer (HSS): a FastAPI + PostgreSQL service deployed on Heroku
(`high-score-server`). Its established bounded context is the leaderboard, serving a Unity
C# client, a vanilla-JS Jinja2 web view, and a React SPA (`leaderboard-frontend/`, mounted at
`/app`; Jinja2 views remain at `/`). Companion game: Flick Fest.

Prefer idiomatic solutions over clever hacky workarounds; document tradeoffs; every architectural decision should be explainable out loud.

## Stack (settled — do not re-litigate)

- **API:** FastAPI, **async** (`async def` handlers throughout — see ADR 0014, which superseded the earlier sync-over-async ADR 0005).
- **DB, leaderboard:** PostgreSQL via **psycopg3 (`psycopg`) in async mode** and raw SQL. Connections come from `psycopg_pool.AsyncConnectionPool` via `async with get_pool().connection() as conn:` (commit on clean exit, rollback on exception). This remains the settled path for existing HSS functionality.
- **Auth:** JWT (HS256) access tokens + opaque refresh tokens (SHA-256 hashed, rotated on refresh via `DELETE ... RETURNING`). Guest accounts created silently via `POST /api/auth/guest`; upgraded in place by `/claim` or by linking a durable external identity. `users` is the canonical account; `auth_identities` stores authenticators such as native `ubear` email/password and Steam.
- **Cache:** pluggable backend with an **async** interface — in-process `cachetools` by default, Redis (`redis.asyncio`) opt-in via `CACHE_BACKEND`. 120s TTL, graceful fallback when the cache is unavailable.
- **Rate limiting:** slowapi.
- **Migrations:** Alembic with explicit reviewed migrations. The leaderboard lineage remains raw SQL with no metadata/autogenerate.
- **Hosting:** Heroku. Prefer existing add-ons (Postgres, Redis). **Do not introduce new infrastructure** without explicit approval.

Stack decisions are settled per bounded context. Do not introduce the
SQLAlchemy ORM. The async/sync boundary is the likeliest source of subtle bugs: any blocking
library call inside a handler must be offloaded (`asyncio.to_thread`, as bcrypt is) or use an
async client (as the Redis cache does) — never call it directly on the event loop.

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
- Explicit error handling with `HTTPException`. Never a bare `except` that swallows. Catch specific psycopg3 errors (`psycopg.errors.ForeignKeyViolation`, `UniqueViolation`; check `e.sqlstate == "23505"`) and map them to clean HTTP statuses.
- Clarity over cleverness — this code is read in interviews.
- **No module-level side effects.** `sys.exit`, `load_environment()`, `logging.basicConfig()` at import scope prevent safe import — guard under `if __name__ == "__main__"`.
- `sort_order` / `period` are interpolated into SQL **only** from DB-sourced or validated-literal values, never raw user input. Preserve that invariant.

## Key invariants (don't break silently)

- `scores` is **upsert-on-best** per `UNIQUE (user_id, game_mode, period, period_start)`. Best-wins respects the mode's `sort_order` via the improvement predicate. Some modes may opt into the separate **cumulative** scoring strategy; keep best-vs-cumulative behavior explicit.
- **Period bucketing:** each submission writes one row per period in `PERIODS`, keyed by `period_start`.
- Guests are real `users` rows (`is_guest=true`); `scores.user_id` is `ON DELETE RESTRICT` — never orphan leaderboard history. Guest pruning checks for score ownership first.
- `game_modes.name` is a natural key (small, stable, human-readable in logs/SQL). The guest-gating column is **`requires_claimed_account`** (blocks guests when true).
- `users` is the durable leaderboard identity. `auth_identities` is the set of ways that identity can be proven. A user may have many identities; `(provider, provider_user_id)` is globally unique and maps to exactly one `user_id`.
- Native email/password auth is represented as provider **`ubear`** in `auth_identities` while the existing `users.email` / `users.password_hash` columns remain for compatibility. Do not add one nullable column per external provider; providers should be data, not schema.
- `auth_identities.provider_user_id` is `TEXT` even when the provider subject is numeric. SteamID64 fits signed BIGINT today, but text keeps Steam, Epic, Google, and future providers behind the same contract.
- Steam auth must validate a client-provided ticket server-side via Steam before resolving or linking an identity. Never trust a client-sent SteamID directly. Steam verification uses async HTTP (`httpx.AsyncClient`) and belongs at the authentication boundary; once resolved, downstream JWT, refresh-token, scores, and runs behavior stays provider-agnostic.

## Working agreements

- **Phased work** with the user in the loop after each phase. Build incremental stability; don't attempt the whole feature in one pass.
- **Section-by-section diffs over full rewrites.** Minimal changes unless a rewrite is clearly warranted. Match the existing module structure rather than proposing new layouts. If a rewrite would provide tangible benefits or simplification, propose it to Human.
- When a decision has two reasonable answers, **enumerate the tradeoffs and surface the fork** — don't resolve it silently.
- **Flag anything that requires an Alembic revision** (now: an Alembic revision) rather than just a code change.
- **Flag any new dependency** with the install command and a reminder to update the lockfile / `pip freeze`.
- **Flag the async/sync boundary explicitly** — it's the likeliest source of subtle bugs here.
- Note whether an approach is **idiomatic or a pragmatic shortcut**.
- **Flag uncertainty** on library/version specifics rather than presenting a guess as fact.
- **ADR discipline:** material architectural decisions get a Nygard-format ADR. Propose ADR stubs when you make such a decision.

## How to run

- Tests: `pytest`
- Lint (CI scope): `./scripts/lint.sh` on Linux/WSL, `.\scripts\lint.ps1` on Windows. Both wrap `ruff check app/ tests/ scripts/` and take `--fix` / `--format` (`-Fix` / `-Format`). They are two ports of one thing — change both together, and keep the target list in sync with the ruff step in `.github/workflows/ci.yml`.
- Dev server: `python run_dev.py` (interactive docs at `/docs`) works everywhere. On Linux/WSL/macOS `uvicorn app.main:app --reload` also works directly, because SelectorEventLoop is already the default there. **On Windows you must use `run_dev.py`** — psycopg3's async pool can't run on Windows' default ProactorEventLoop, and uvicorn builds its loop before importing the app, so the policy has to be set in the launcher first. `tests/conftest.py` sets the same policy for the test suite.
  - That policy code is guarded by `sys.platform == "win32"` in `run_dev.py`, `tests/conftest.py`, and several `scripts/`. It is a deliberate no-op on Linux — **do not delete it as dead code** when working from WSL.
- **The vault MCP server is not a separate process and has nothing to start.** It is an ASGI app mounted into the host application at `/api/v1/vault/mcp/`, behind the same `VAULT_ENABLED` gate as the vault's HTTP routes. It is therefore already running wherever the host runs: under `run_dev.py` locally, and under gunicorn on Heroku. There is no Procfile entry for it and there should not be one.
  - Registering it with a client is a URL registration, not a launch. `claude mcp add --transport http` only stores the URL and header; nothing is spawned. (Only stdio-transport servers, `claude mcp add <name> -- <command>`, are spawned by the client — the vault does not offer one.)
  - **Which tools a client sees is decided by its credential's scopes**, so issue a credential per client with `scripts/issue_vault_credential.py` and grant the narrowest set that client needs. Withholding `vault:update`/`vault:delete` is a prompt-injection boundary, not just tidiness — see vault ADR 0021.
  - Note the trailing slash: `/api/v1/vault/mcp/`. The bare form 307-redirects to it, so both work, but the slash saves a hop.
  - **A change to the MCP surface needs no client action; a change to the `knowledge-vault` skill needs one per client.** The tool list and server instructions are fetched from the deployment, so a deploy reaches everyone; the skill is a directory on each client's disk, owned by the private knowledge-platform repo, and nothing pulls it. **Agreement is a separate problem from distribution, and it is symmetric:** the two live in different repositories with no shared release and no CI check, so either can be the one that moved first — this repo adding a tool the skill does not know, or the skill describing a tool that has not merged here. A change to the tool surface on either side is not finished until the other side has been read. Steps for both, and why a skill cannot update itself, are under "Shipping a change to clients" in `app/vault/docs/vault-configuration.md`.
- `run_mcp.py` serves **only** the MCP app, on its own port, without the leaderboard routes, SPA, or static mount. It is a debugging aid with a narrow purpose: isolating whether a transport-level failure belongs to the adapter or to the host's routing and middleware. **It is not needed for ordinary use** — `run_dev.py` already serves the same endpoint — and it is not a deployment target.
- DB schema is managed by **Alembic** (raw-SQL migrations; baseline `0001_baseline` reflects the pre-Alembic schema). `db/schema.sql` is a labeled bootstrap snapshot, not the source of truth.
  - Fresh/empty DB (CI, new clone): `alembic upgrade head` builds the whole schema.
  - Existing DB that predates Alembic: `alembic stamp 0001_baseline` once (never `upgrade` — the objects already exist), then `upgrade head` for later revisions.
  - `env.py` reads `DATABASE_URL`, loading `.env` with `override=False` — so a process-env URL (a throwaway DB, or prod) overrides `.env`. Always print the value before a stamp/upgrade against a non-default target: `echo $DATABASE_URL` in bash, `echo $env:DATABASE_URL` in PowerShell.
  - For local migration tests, remember that pytest points the app at `TEST_DATABASE_URL`, but Alembic reads `DATABASE_URL`. Override `DATABASE_URL` to the test URL before `alembic upgrade head` when preparing the test database.
- Deploy (Heroku): migrations run automatically via the Procfile `release: bash scripts/release.sh` phase; a failed migration aborts the release. No manual migration step on deploy. The script runs the leaderboard lineage always and the vault lineage only when `VAULT_ENABLED=true` — the vault's first migration runs `CREATE EXTENSION vector`, so running it unconditionally would make every deploy depend on pgvector being available on the plan.
- DB ad-hoc, Heroku: `cat file.sql | heroku pg:psql --app high-score-server` (`Get-Content` instead of `cat` in PowerShell), or `heroku run alembic current --app high-score-server`.
- Steam auth is optional and configured through `STEAM_WEB_API_KEY`, `STEAM_APP_ID`, and `STEAM_AUTH_IDENTITY`. `STEAM_WEB_API_KEY` must stay server-side only; never log or expose this.
- **Dev environment: determine it, don't assume it.** As of 2026-08-15 the primary environment is **WSL2 (`Ubuntu-24.04`)**, migrated from Windows/PowerShell to get command sandboxing; Windows remains supported and its constraints below are still live. Check `uname -s` (or the platform your harness already reports) before choosing shell syntax — this section describes both environments, and the wrong half is actively misleading.
  - *Linux / WSL:* an ordinary POSIX shell. `curl`, `< file` redirection, `$VAR`, and `cat` all behave normally. Venv: `source .venv/bin/activate`.
  - *Windows / PowerShell:* `curl` is an alias for `Invoke-WebRequest` (throws on non-2xx) — use `curl.exe`. Stdin redirection (`< file`) is unsupported — use `Get-Content file | heroku pg:psql`. Env vars set with `$env:` are per-process and do not persist across shells. Venv: `.venv\Scripts\Activate.ps1`.
  - Either way, `.env` is loaded by the app and by Alembic's `env.py`, **not** by the shell.
- **Keep the repo on the Linux filesystem.** Under WSL it lives at `/home/ubuntu/projects/HighScoreServer` (ext4). A checkout under `/mnt/c` goes through DrvFs, which is slow and does not carry POSIX permission bits faithfully. The reliable test is the path prefix — `case "$PWD" in /mnt/*) ... ;; esac` — not the filesystem type, which a sandbox can mask.

## Scope guardrails for current work

See `docs/specs.md` for the validated-runs / cumulative-scoring spec, and ADR 0015 plus migration `0004_auth_identities` for external identities. Deferred / out of scope unless explicitly raised: asyncpg (the async migration landed on psycopg3 — see ADR 0014; asyncpg specifically remains out of scope); SQLAlchemy ORM; converting slowapi's rate-limit storage to async; server-issued seeds; normalized per-action tables (blob is used instead); admin review UI; password reset; React integration of runs; additional external providers beyond Steam unless requested.

<!-- BEGIN vault-context — delete this block when app/vault/ is extracted -->
### `app/vault/`

`app/vault/` and `vault_migrations/` are maintained separately and will be moved out.
Read `app/vault/AGENTS.md` before changing anything under them.

Rules that differ from the rest of this repository:

- SQLAlchemy Core, not raw SQL. ADR 0002 does not apply inside `app/vault/`.
- No imports between vault code and leaderboard routes, models, repositories, or tables,
  in either direction.
- No cross-schema foreign keys, views, triggers, shared sequences, or cross-domain
  transactions.
- Documentation and ADRs for this code live in `app/vault/docs/`, never in `docs/`.
- `pgvector` is used only here and leaves with the package.
<!-- END vault-context -->
