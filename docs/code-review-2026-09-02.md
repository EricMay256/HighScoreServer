# Code review — 2026-09-02

A full-tree review of `dev` at `e8634b2`. Read: everything under `app/` and
`app/vault/`, both Alembic lineages, `scripts/`, the console templates and the
shared session script, the React SPA, the Jinja2 views, CI, the Procfile and
release script, and the test fixtures. Ran: `ruff check app/ tests/ scripts/`
(clean), `ruff check .` (nine auto-fixable findings outside the CI scope), and
the full pytest suite (see *Verification*).

Findings are grouped by area and ordered by severity within each. Each names the
location, what is wrong, why it matters, and the fix. Size is a rough estimate:
**S** minutes, **M** an hour or two, **L** a session. Nothing found is a
data-loss or authentication-bypass defect. The code is careful where it
matters — refresh-token rotation, the corpus lock discipline, the OAuth replay
handling, the scope-filtered tool list and the fail-closed read policy all hold
up. The findings are hygiene, drift, and a few edges.

## The five to do first

1. Stop returning internal exception text on `500` responses (L1).
2. Pin the Python runtime for Heroku, after confirming what the buildpack
   currently uses (O1).
3. Re-encode `requirements-dev.txt` as UTF-8 and move the dev tools out of the
   production install (O2).
4. Regenerate the SPA's API types; `requires_auth` has not existed since ADR
   0008 (F1).
5. Stop the review console fetching every evidence note in full while rendering
   the cases queue (V1).

## Leaderboard

### L1. `500` responses echo the underlying exception — Medium, S

`app/auth_routes.py:127, 175, 199, 277, 320, 379` and
`app/leaderboard_routes.py:69, 114, 298, 435, 614` raise
`HTTPException(500, detail=str(e))` from a broad `except Exception`. A psycopg
error message names tables, columns and constraints and can include parameter
values; a pool timeout names the pool. That text reaches an unauthenticated
client. The handlers already log the exception, so nothing is lost by returning
a fixed string.

Fix: `detail="Internal server error"` at every site, keeping the `from e` and
the log line. `leaderboard_routes.py:191` is different — a `400` carrying the
action-log validator's own message — and is fine as it is.

### L2. The API key is compared with `!=` — Low, S

`app/dependencies.py:15`. Use
`hmac.compare_digest(x_api_key.encode(), expected.encode())`. The vault's
`auth.secret_matches` already does this; the leaderboard should match. A timing
attack on this over Heroku's router is impractical, but it is a one-line fix an
interviewer will ask about.

### L3. `/latest`'s `total_count` is unfiltered only on the empty-page fallback — Low, S

`app/leaderboard_routes.py` (`latest_scores`): the windowed `COUNT(*) OVER ()`
respects a `game_modes` filter, but when `offset` lands past the last row the
fallback `_count_all_scores()` counts every score. The README's Known
Limitations described the count as always unfiltered, which was wrong in the
other direction. Fix: pass the filter into the fallback count. The README was
corrected in this pass to describe the actual behaviour.

### L4. The default game mode `blitz` is hardcoded and absent from the seed — Low, S

`templates/base.html:19`, `templates/home.html:12` and
`leaderboard-frontend/src/App.tsx:21, 38` assume a mode named `blitz`;
`db/seed.sql` seeds `classic`, `speedrun` and `challenge`, and
`app/view_routes.py` defaults to `classic`. A fresh local database shows an
empty "BLITZ MODE". Fix: pick the first configured mode (the SPA already
fetches `/game_modes`), or seed `blitz`.

### L5. `SlowAPIMiddleware` is registered with no default limits — Low, S

`app/main.py:138`. With no `default_limits` on the `Limiter`, the middleware
covers nothing; the per-route decorators do the work. Harmless, but it reads as
global limiting. Either set `default_limits` deliberately or drop the middleware
and say so in a comment.

### L6. ADR 0007's single-process premise no longer matches the Procfile — Low, doc

The Procfile runs two Gunicorn workers, so the per-process cache and limiter
degradation the README listed as a *future* trigger is the live state: per-IP
limits are effectively doubled, and a cache invalidation on one worker leaves
the other's copy for the TTL. Accepted at current traffic. The README's Known
Limitations were updated in this pass to say so; ADR 0007 itself is immutable
and its consequences section already describes the degradation.

### L7. Rename does not reissue tokens — Low, S

`POST /api/auth/rename` leaves the JWT's `username` claim stale until the next
refresh; `RenamePanel.tsx` carries the TODO. Returning a fresh `TokenResponse`
from `/rename` is the small server-side change.

> **Corrected 2026-09-03. Do not follow the recommendation above.** Returning a
> full `TokenResponse` is what was implemented first, and it was wrong:
> `create_refresh_token` inserts without revoking and `/rename` accepts no
> refresh token to rotate, so every rename minted a second live credential and
> left the first valid. Measured at five renames: 1 refresh-token row became 6,
> with the original still accepted by `/refresh`, and nothing bounded it.
>
> A refresh token is opaque and carries no username, so a rename does not
> invalidate it and there is nothing to reissue. The shipped contract is
> `AccessTokenResponse` — the access token alone, with the caller's refresh
> token untouched. `/claim` returns a full pair and is safe from this only
> because it can succeed once per account, which is what made it a misleading
> model to copy.

## Vault

### V1. The review console fetches every evidence note while rendering the cases queue — Medium, M

`app/vault/templates/review.html:501` awaits `GET /notes/{id}` for every
similar note of every pending review case, sequentially. `get_note` is
quota-limited at 120/min with a burst of 30 per principal, so a handful of
cases with five evidence notes each exhausts the burst and the remaining cases
render as errors. The amendment queue was fixed for exactly this shape —
previews travel with the queue — and the cases tab was not. Low frequency
today, since at `flag_at = 1.0` only an exact resubmission flags, but it is the
surface a reviewer meets when it matters. Fix: render evidence from the case's
stored `similar` (it already carries id, title and score) and fetch a body only
on expand, or carry a bounded preview in `VaultReviewCaseSummary` as the
amendment queue does.

### V2. The `importer` quota override and the runbook disagree about principal naming — Low, S

`app/vault/rate_limit.py:159` widens `contribute` and `update` for the literal
principal `importer`, citing the old handoff's rule that the importer must run
under that name. The runbook's corpus-migration procedure ("Issue a fresh
import principal") now deliberately issues a new principal per re-import, so
the override applies only when that principal happens to be named `importer`.
Not a bug — a fresh principal runs at the base 30/min — but a 500-note
re-import under `importer-c` takes four hours for no protective gain. Fix:
match the override on a prefix (`importer`, `importer-*`) or tell the runbook
to name the fresh principal `importer-<date>` and match on that. The comment in
`rate_limit.py` was updated in this pass to state the current situation.

### V3. Narrowing scopes on refresh is persisted to the grant — Low, doc

`app/vault/oauth.py:750-755` (`_issue`): the refresh path honours a narrower
`scope` request and writes the narrowed set to
`vault_oauth_grants.authorized_scopes`. The SDK requires refresh scopes to be a
subset of the token's, so a family that narrows once can never widen again
without re-authorizing. OAuth semantics permit this; it is just undocumented.
Fix: a sentence in ADR 0029's consequences or the runbook, or project the
narrowing onto the credential only and leave the grant alone.

### V4. `load_access_token` compares secret digests with `!=` — Low, S

`app/vault/oauth.py:603`. `auth.secret_matches` uses `hmac.compare_digest`;
this path, which serves the SDK's `/revoke`, does not. Use the same helper.

### V5. The console's `credentialId()` splits the token from the left — Low, S

`app/vault/templates/_console_session.js:441` takes `TOKEN.split("_")[1]`,
while the server and `AGENTS.md` split from the right because ids may contain
`_`. Every id minted today is hex, so it works. Prefer `IDENTITY.credential_id`
from `GET /authorization` once it has loaded, with the split as the fallback.

### V6. `vault_search`'s `limit` bound is enforced but not declared — Low, S

`app/vault/mcp.py:557`: `limit: int = 10`, with a runtime check for 1–50. The
`query` parameter declares its bound with `Field(max_length=…)` precisely so a
generated client can see it; `limit` should do the same with
`Field(ge=1, le=50)`.

### V7. Console CSP allows `'unsafe-inline'` scripts — Low, M

`app/vault/console_page.py:41`. The pages inline their script, so the directive
is required as written. A per-response nonce would let
`script-src 'self' 'nonce-…'` replace it. Defence in depth only; the pages
build their DOM with `textContent`.

### V8. `_summary_still_repairable`'s signature is mis-indented — Low, S

`app/vault/service.py:392`: the first parameter sits at column 0. Valid Python,
and ruff does not flag it, but it reads as a paste error. Indent it.

### V9. Stale docstrings and comments — fixed in this pass

- `app/vault/mcp.py`: the module docstring said the vault has no authorization
  server (it has one since ADR 0024); `build_vault_mcp_server` said "fifteen
  tools" while eighteen are registered — the count is no longer stated.
- `app/vault/routes.py`: the module docstring said review, compile and export
  endpoints are deliberately absent; review and compile exist.
- `app/vault/service.py:104` said compilation was a NEXT-STEPS item; `:1056`
  said nothing deletes documents.
- `app/vault/AGENTS.md`: `Agent/wiki/` described as unowned (it joined
  `CORPUS_OWNED_PATH_PREFIXES` on 2026-08-24); the `token_verifier` paragraph;
  two references to task numbers in a handoff that is now archived.
- `app/vault/docs/vault-configuration.md`: the "nothing here has been applied"
  banner; the lineage head; "14 tools".
- `app/vault/docs/vault-architecture.md`: "Status: implementation plan";
  "`export.py` does not exist"; the leaderboard pool described as 10 per
  worker.

## Frontend

### F1. `leaderboard-frontend/src/api/types.ts` has drifted from `app/models.py` — Medium, S

Lines 34 and 41 declare `requires_auth`, renamed to `requires_claimed_account`
in ADR 0008. `GameModeConfig` also lacks `required_tier`, `scoring_strategy`,
`game_key` and `max_score`; `ScoreResponse` lacks `validated` and
`validation_tier`; `ScoreSubmission` lacks `idempotency_key`. Nothing reads
`requires_auth`, so there is no runtime bug, but the file's own header claims
it mirrors the server. Fix: regenerate from `/openapi.json`, or hand-correct,
and note which revision it was generated from.

### F2. Build tooling is listed under `dependencies` — Low, S

`leaderboard-frontend/package.json` puts `typescript`, `vite`,
`@vitejs/plugin-react` and the `@types/*` packages in `dependencies`. Both the
root `package.json` and the frontend one define `heroku-postbuild`, and the
frontend's runs `npm install` a second time. It works, but it is not what a
reviewer expects. Move the tooling to `devDependencies` (Heroku's Node buildpack
installs them) and keep one `heroku-postbuild`.

> **Corrected 2026-09-03. The parenthetical above is wrong and following it
> breaks the build.** The buildpack installs dev dependencies for *its own*
> root install, not for the nested `npm ci` that the root `heroku-postbuild`
> runs inside `leaderboard-frontend/`. Heroku builds with `NODE_ENV=production`,
> under which `npm ci` omits `devDependencies` — verified on a clean
> `node_modules`, where neither `vite` nor `tsc` was installed and the build
> had nothing to run.
>
> Moving the tooling is still right; it needs `npm ci --include=dev` in the
> root build script, which is what shipped.

### F3. Tokens live in `localStorage` — Low, informational

`leaderboard-frontend/src/auth/store.ts`. Standard for a portfolio SPA and
consistent with the Unity client's `PlayerPrefs`; the cost is XSS exposure of
the refresh token. Worth a sentence in the README's Known Limitations rather
than a change.

## Operations and tooling

### O1. No Python version is pinned for Heroku — Medium, S

There is no `.python-version` and no `runtime.txt`, yet the README's project
tree listed `runtime.txt` and `pyproject.toml` says "CI and Heroku are 3.12".
CI pins 3.12; the local venv is 3.14.4 and the suite passes there, so the code
runs on both. But the Heroku buildpack picks its own default when nothing is
pinned, and the `target-version` reasoning in `pyproject.toml` depends on the
deployed interpreter being the oldest one. Fix: commit `.python-version`
containing `3.12` and update the README tree. **Confirm what the buildpack
reports at build time** (`Using Python 3.x` in the build log) before assuming
3.12; this review did not query Heroku.

### O2. `requirements-dev.txt` is UTF-16, and `requirements.txt` carries the dev tools — Medium, S

`requirements-dev.txt` is UTF-16 LE with a BOM (`cat` shows
`r e q u i r e m e n t s`). pip's `auto_decode` handles the BOM, which is why
CI passes, but `grep`, Dependabot and a reviewer's editor do not, and
`.gitattributes` treats the file as text. It also re-pins `ruff` and `pytest`,
both already in `requirements.txt` alongside `coverage`, `pytest-cov` and
`pytest-asyncio`, so the production slug installs the test toolchain. Fix:
rewrite the file as UTF-8, keep it as `-r requirements.txt` plus the dev-only
pins, and drop the dev tools from `requirements.txt`.

### O3. The Procfile uses the deprecated `uvicorn.workers.UvicornWorker` — Low, S

Deprecated since uvicorn 0.30 in favour of the `uvicorn-worker` package; still
present in the pinned 0.42, so nothing is broken. Switching is
`pip install uvicorn-worker` (a new dependency — pin it) and
`-k uvicorn_worker.UvicornWorker`.

### O4. The lint scope leaves nine fixable findings outside CI — Low, S

`ruff check .` reports `I001` import order in `migrations/env.py`, the four
leaderboard revisions and `run_dev.py`, and `W292` in `wsgi.py`, all
auto-fixable. One `--fix` pass, then add those paths to `scripts/lint.*` and
the CI step together.

### O5. Counts in `pyproject.toml` are stale — Low, doc

The `E501` note says 116; `ruff check --select E501` reports 202 today. Not a
gate; worth its own pass.

### O6. `asyncio.set_event_loop_policy` is deprecated on 3.14 — Low, later

`run_dev.py` and `tests/conftest.py` need the policy because uvicorn and anyio
create the loop; the scripts already use `loop_factory`. Nothing to do before
3.16 removes it, but the warning filters in `pyproject.toml` are hiding a
deadline.

## Documentation — done in this pass

- Consolidated `docs/NEXT-STEPS.md`; rewrote `docs/HANDOFF.md` as a start-here;
  archived five historical handoffs under `docs/archive/` with banners and an
  index; renamed the librarian handoff to `librarian-plan.md` (moved to
  `app/vault/docs/` on 2026-09-03, being vault documentation) and recorded
  that Phase 0 is done.
- README: the vault is no longer described as dark in production; the stale
  "configuration since the last merge" section was replaced; the duplicate
  `## Deployment` header, the `release:` line, `runtime.txt`, the project tree,
  the auth and leaderboard route tables, the cache note's "single worker", and
  the `/latest` count claim were corrected.
- The vault runbook, architecture doc, package `AGENTS.md`, root `AGENTS.md`,
  and the code comments in V9 were updated.

## Verified sound

Called out so the next reviewer does not redo them: refresh rotation via
`DELETE … RETURNING`; bcrypt offloaded with `asyncio.to_thread` on both sides;
`sort_order` and `period` reach SQL only from database rows or validated
literals; the corpus advisory lock brackets every governed write and embedding
happens outside it; OAuth replay burns the whole family and the protocol errors
are raised outside the transaction; nonces, codes and CSRF tokens are stored as
digests and redeemed with `DELETE … RETURNING`; the MCP tool list is
scope-filtered and each tool re-checks; the read policy fails closed on unknown
paths; migrations carry no grants and the release phase gates the vault
lineage; the export prunes only owned prefixes; `search_response` trims to a
byte budget shared by both adapters; the span edit round-trips through the
strict applier before it is stored.

## Verification

- `ruff check app/ tests/ scripts/`: clean. `ruff check .`: 9 findings, all
  auto-fixable, all outside the CI scope (O4).
- Full suite (`pytest -q`, Python 3.14.4, local PostgreSQL 17 with pgvector):
  **1,334 passed, 4 failed, 174 errors in 7m02s**. Every failure and error was
  `FATAL: the database system is in recovery mode` or
  `not yet accepting connections`: the local PostgreSQL server restarted about
  a third of the way through the run and recovered, and the affected modules
  were the ones scheduled in that window. Not a code regression.
- Targeted rerun of the eleven affected modules on the recovered database:
  **207 passed in 63s**. Combined with the first run, every test in the suite
  has passed once.
- Not verified: anything about the Heroku deployment — config vars, the
  running Python version, the applied migration head. Those are stated only as
  the documents last recorded them.
