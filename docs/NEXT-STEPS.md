# Next steps

Current as of 2026-11-26. The vault's roadmap moved to `knowledge-vault`; the leaderboard sections were last revised 2026-09-12. One ordered list of what remains, across the
leaderboard and the vault, with the *why now* and the blocking relationships.
Detail lives elsewhere and is linked; this file exists so nothing else has to
be read to know what to pick up.

Where the durable records are: the [ADR index](adr/README.md) (the vault's moved to
`knowledge-vault` with it), [`specs.md`](specs.md)
for validated runs, and [`HANDOFF.md`](HANDOFF.md) for the start-here context.
Historical handoffs are under [`archive/`](archive/README.md).

## State

- **`main` is level with `dev` as of PR #35** (2026-11-26, squash-merged as
  `697df70`), so `main` is not an ancestor of `dev` and the next merge is
  another PR. Production runs it, with vault migration 0021 applied by the
  release phase.
- **Production (2026-11-26):** the composed deployment, built from `knowledge-vault` with
  this repository's `main` fetched at build. Release v91: `knowledge-vault` `729acfc`,
  HSS `697df70`. Leaderboard lineage `0004_auth_identities`.
- **Suite:** 1,373 tests, about seven minutes on this machine against a local
  PostgreSQL 17 with pgvector. Never run two pytest processes against the test
  database at once.
- **Lint:** `ruff check .` is clean. The gate is the whole tree as of
  2026-09-02 — the old `app/ tests/ scripts/` scope left `migrations/`,
  `run_dev.py` and `wsgi.py` unlinted.

## The vault — moved to `knowledge-vault`

The vault was extracted to the private `knowledge-vault` repository on 2026-11-25, and
production has run the composed deployment from it since 2026-11-26 (vault ADR 0062,
host ADR 0016). **Its roadmap and handoff live there now**, in
`knowledge_vault/docs/NEXT-STEPS.md` and `knowledge_vault/docs/HANDOFF.md`. Those
cover the agent loop, corpus quality and retrieval, the Human vault, the retrieval freeze,
and the vault's deferred decisions. The full text of this file's vault sections, as they
stood before the move, is in git history (`3fc14b3`).

What remains *here*:

1. **Delete the frozen vault copy** once the composed app has served real traffic, per
   the manifest's "HSS files that need editing":
   - `app/vault/`, `vault_migrations/`, `alembic-vault.ini`, the vault scripts and
     `tests/vault/`;
   - `composed.py`, with `wsgi.py` and `run_dev.py` going back to `app.main:app`;
   - the vault block in `scripts/release.sh`;
   - `pgvector` and `mcp` in `requirements.txt`;
   - the vault environment and pgvector image in CI;
   - the vault sections of `README.md`, `AGENTS.md` and `.env.example`.

   Keep `tests/vault/test_hss_pool_config.py`, moved out of `tests/vault/`, and keep the
   reverse boundary check (no `app/` module imports the vault). Until the copy is deleted,
   **do not change it**; it exists only so `heroku rollback v89` still works.
2. **Production is built from `knowledge-vault`.** A push to HSS `main` changes
   production only when that app next builds. The operator repointed the GitHub
   integration on 2026-11-26.

## 1. Leaderboard — immediate

1. **Settle the production game-mode list.** Nothing hardcodes a mode any more, so
   `/leaderboard` and the SPA both land on the first row of `/game_modes`, ordered by
   `name` — the alphabetically first configured mode *is* the landing page. `blitz` sorts
   first in production as of 2026-11-26; unused modes are still to be pruned. The failure
   is silent: a stale or test mode sorting earlier becomes the front page and nothing
   errors.

## 2. Leaderboard

- **Access-token revocation via a JTI denylist.** Insertion points are marked
  `# DENYLIST HOOK`; needs a shared store (Redis) and a decode-time check.
  Trigger: a known compromise, or a claim flow that must invalidate the guest's
  prior tokens.
- **Retention policy for guests with score history.** Scoreless guests are
  pruned; guests with history need a decision on how long, and whether to tell
  players.
- **Password reset.** Token storage, email delivery, endpoints, UI.
  `users.email` is already nullable, so the schema is ready.
- **Game-to-mode ownership.** `game_modes.game_key` exists and nothing reads
  it; promote it to a first-class grouping when a second game ships.
- **Validated runs, deferred pieces** ([`specs.md`](specs.md)): a `min_score`
  floor, a max-duration bound (needs typed action semantics), tier-3
  deterministic replay, React integration of `/runs`.
- **Rename's remaining client work.** `/rename` reissues tokens as of
  2026-09-02, and the SPA and Unity client store them. The C++ client needed a
  separate fix — it sent the wrong field name and had never worked against a
  real server. Left: the Unreal client does not implement rename at all, and
  the Unity change is untested against a real editor build.
- **Cursor pagination for `/latest`** if a client ever needs stable feed paging
  under inserts.

## 3. Tooling and hygiene

- **Split the test suite by domain** (`tests/vault` against the rest) so
  feedback rounds on leaderboard work skip the seven-minute run.
- **An `E501` pass.** 196 findings across the tree, 27 of them in
  `app/vault/`; `pyproject.toml` records that count and the date it was taken.
  Worth its own change, not a gate.
- **`UP042`**: rewrite the `(str, Enum)` classes as `StrEnum` deliberately, with
  tests — `str()` semantics differ on shipped API fields.
- **Procfile worker class.** `uvicorn.workers.UvicornWorker` is deprecated in
  favour of the `uvicorn-worker` package; switching adds one dependency and is
  a one-line Procfile change.
- **Python 3.14 deprecations** filtered in `pyproject.toml`
  (`set_event_loop_policy`) need a `loop_factory`-based launcher before 3.16.
  The scripts already use it; `run_dev.py` and `conftest.py` cannot until
  uvicorn and anyio expose the seam.
