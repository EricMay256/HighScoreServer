# 16. The deployed application is two apps behind a path dispatcher

Date: 2026-11-25

## Status

Accepted

## Context

Until now the staged vault was wired into the leaderboard's own FastAPI
application. `app/main.py` started the vault's engine, embedding provider and MCP
session manager in its lifespan. It also registered the vault's REST router,
exception handler, MCP mount and OAuth and console routes, all ahead of the SPA
catch-all. That made `app/main.py` the one leaderboard module allowed to import
the vault.

Vault ADR 0062 schedules extraction. A private `knowledge-vault` repository will
become the Heroku deploy root and compose HSS's public `main` at build. Whatever
does the composing has to reach everything the vault needs from outside HSS.

Three shapes were weighed:

- **Plugin hooks in `create_app`.** One application. HSS takes on an extension
  API it must keep stable, and the vault still has no application of its own, so
  its tests keep needing all of HSS.
- **The vault app as the outer app, with HSS mounted at `/`.** HSS needs no new
  API, but any middleware the vault adds wraps every leaderboard request, and the
  public product becomes a sub-application of the private one.
- **Two independent applications behind a dispatcher.** Each keeps its own
  middleware, exception handlers, state and lifespan, and either can be deployed
  alone.

## Decision

**Two independent ASGI applications, one process, dispatched by path.**

- `app/main.py`'s `create_app()` builds the leaderboard alone and imports nothing
  from the vault. `app.main:app` serves the leaderboard on its own.
- `app/vault/application.py`'s `create_vault_app()` builds the vault alone, at the
  same absolute paths as before. It owns its lifespan, its exception handlers and
  its API docs, under `/api/v1/vault/docs`.
- `composed.py`, at the repository root, holds `ComposedApp`. A request goes to
  the vault when **any vault route matches its path**, fully or by path alone, and
  to the leaderboard otherwise. Lifespans nest with the leaderboard outermost.
  While the vault is disabled, `composed.app` is the leaderboard app itself and
  the vault is never imported.
- `wsgi.py` and `run_dev.py` serve `composed:app`.

The ownership set is the vault's live route table, never a hand-kept prefix list.
`tests/vault/test_composition.py` asserts that no concrete leaderboard route is
owned by the vault. The single application registered leaderboard routes first,
so a collision would have gone the leaderboard's way; the dispatcher asks the
vault first. With no collisions, the two orders are equivalent. That test caught
one on its first run: FastAPI's default `/docs/oauth2-redirect`, which the vault
app now registers under its own prefix.

## Consequences

- **The leaderboard's CORS policy no longer wraps vault routes.** No cross-origin
  browser calls them: the consoles are same-origin and connectors call
  server-to-server. The operator confirmed that across every origin involved.
- **The vault registers its own `RateLimitExceeded` handler.** Its REST pre-auth
  guard raises from a dependency, and only the leaderboard's handler used to turn
  that into a 429. Without its own, the vault would answer 500. The body and
  headers match byte for byte, and a test pins the equivalence.
- `/docs` describes the leaderboard only. The vault's API is at
  `/api/v1/vault/docs`.
- `.env` loading and Sentry start in the leaderboard's lifespan, which runs first.
  A vault-only deployment would need its launcher to do both.
- **Test harness.** `composed_client` owns the session's only lifespan.
  `init_vault_db` refuses a second engine, and the engine is bound to one event
  loop. `client` aliases `composed_client`. `vault_client` targets the vault app
  inside the composed one and shares `composed_client`'s portal.
  `TestClient.portal` is public, and `_portal_factory` reuses it when set. Modules
  that exercise only vault routes override `client` with `vault_client`, so they
  hold against the vault app alone and move with the package unchanged.
- `tests/vault/test_boundaries.py`'s host allowlist is now empty: no module under
  `app/` may import the vault.
- At extraction, `composed.py` moves to `knowledge-vault` with only the vault's
  import path changed, and HSS's `wsgi.py` serves `app.main:app`.
