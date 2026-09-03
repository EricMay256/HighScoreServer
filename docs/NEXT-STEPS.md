# Next steps

Current as of 2026-09-02. One ordered list of what remains, across the
leaderboard and the vault, with the *why now* and the blocking relationships.
Detail lives elsewhere and is linked; this file exists so nothing else has to
be read to know what to pick up.

Where the durable records are: the two ADR indexes ([leaderboard](adr/README.md),
[vault](../app/vault/docs/adr/README.md)), the
[vault runbook](../app/vault/docs/vault-configuration.md), [`specs.md`](specs.md)
for validated runs, and [`HANDOFF.md`](HANDOFF.md) for the start-here context.
Historical handoffs are under [`archive/`](archive/README.md).

## State

- **`dev` is 94 commits ahead of `main`, and `main` is an ancestor**, so the
  merge is a fast-forward. The range carries two vault migrations,
  `0018_metadata_amendments` and `0019_oauth_grant_label`, which the release
  phase runs on deploy. Behaviour changes to name in the PR: search no longer
  returns bodies (vault ADR 0031); metadata is a proposal kind of its own
  (ADR 0036); the landing page and two consoles exist at `/vault`,
  `/vault/review` and `/vault/browse` (ADRs 0037, 0039); batch amendment
  decisions; operator labels on authorizations (ADR 0040).
- **Production, as last recorded on 2026-08-28** and not re-verified since:
  vault lineage `0017_oauth_entitlements`, 94 documents (80 notes, 14 wiki
  pages), all active; `VAULT_ENABLED`, `VAULT_PUBLIC_URL` and an operator
  identity method set; contributions arriving under per-session OAuth
  principals.
- **Suite:** 1,373 tests, about seven minutes on this machine against a local
  PostgreSQL 17 with pgvector. Never run two pytest processes against the test
  database at once.
- **Lint:** `ruff check .` is clean. The gate is the whole tree as of
  2026-09-02 — the old `app/ tests/ scripts/` scope left `migrations/`,
  `run_dev.py` and `wsgi.py` unlinted.

## 1. Immediate

1. **Merge `dev` into `main`.** Fast-forward, two migrations in range (above).
   Documentation on a non-default branch is documentation nobody reads, and
   GitHub shows `main`.
2. **Settle the production game-mode list.** Nothing hardcodes a mode any
   more, so `/leaderboard` and the SPA both land on the first row of
   `/game_modes`, ordered by `name` — the alphabetically first configured mode
   *is* the landing page. `blitz` is expected to sort first and unused modes
   are to be pruned, but neither has been confirmed against the deployment.
   Worth doing before the merge, because the failure is silent: a stale or
   test mode sorting earlier makes it the front page and nothing errors.

   The buildpack's interpreter was the other half of this item and is settled
   — 3.12, matching the pin. Still unchecked from the same visit: `heroku
   config` and the applied vault lineage head; the production facts above are
   from 2026-08-28.
3. **Backfill note summaries.** 67 of 80 production notes lacked one on
   2026-08-28. `summary` joins the embedding text and is the search preview
   (ADR 0031), so an unsummarized note is measurably harder to find.
   `scripts/backfill_vault_summaries.py` plans and applies; an agent writes the
   prose. Intake is fixed (ADR 0035), so this is back-catalogue only.
4. **Decide whether the four largest notes want splitting.** Contributed
   2026-08-28 at 10.7k–15.4k characters against a corpus mean of 2.1k. Editorial
   before technical: is each one insight or several?
   `scripts/measure_chunk_eligibility.py` reports the population ADR 0034
   watches.

## 2. The librarian and proposal revision

The largest remaining vault feature. The plan is
[`librarian-plan.md`](../app/vault/docs/librarian-plan.md); the decisions are vault ADR 0044
(Accepted, unimplemented) and ADR 0043 (Proposed).

1. **Phase 1 — proposal lineage (ADR 0044).** One Alembic revision on the vault
   lineage adding `revises_proposal_id` (an unconstrained correlation, not a
   self-foreign-key) and an amendment-specific `superseded` state; Core
   metadata, domain and API models, repository and service methods, REST
   routes, the two-credential review-page flow with its recoverable
   "successor filed; finish settlement" state, and tests. Independent of any
   model integration.
2. **Accept or amend ADR 0043**, then Phases 2–8: the workflow tables
   (sessions, events, artifacts, invocations), the `/vault/librarian` console,
   the external-MCP runner and its `vault:librarian` / `vault:librarian-run`
   scopes, the approval adapters, the optional provider-API runner with its
   hard USD 5 monthly ceiling, bounded heartbeat, and the verification corpus.
   Every scope change touches the OAuth constraints, grants, refresh tokens,
   constants, the CLI and the schema-drift tests together.
3. **Open decision:** the first provider/model pair for the sub-USD-5
   experiment, chosen against current official pricing at implementation time.

## 3. Corpus quality and retrieval

- **Validate the retrieval label set.** Every case in
  `app/vault/retrieval_cases.py` is `validated=False`, authored by the same
  agent that reshaped the search response. A human pass over the relevant sets
  is what turns `scripts/measure_retrieval_quality.py` from a self-assessment
  into a regression signal.
- **The exporter and the governance linter disagree on frontmatter key order.**
  Every fresh export presents as roughly 25 files of fixable drift, and
  knowledge-platform's pre-commit hook blocks until `lint --fix` runs.
  Format-only, but manual and cross-repository. Teach the exporter the linter's
  order, or have the export script run the fixer.
- **Facet vocabulary.** `FACET_NAMES` is `{project, area, system}`; the
  2026-08-13 census found half the corpus tagged `gotcha`, a *kind* rather than
  a topic, with nowhere to go. Decide on a `kind`/`genre` axis on queryability
  alone — the dedup-margin argument is settled, tags stay in the embedding
  text. Moving a tag to a facet re-embeds every affected note; do it in one
  pass.
- **A tag census endpoint** (`GROUP BY unnest(tags)`) is the cheapest way to
  make vocabulary drift visible. Independent of everything else.
- **`flag_at` stays 1.0.** Measured, not provisional: the bands overlap on
  `text-embedding-3-small`. The remaining lever is a different model, and that
  is a calibration run recorded in
  [`embedding-calibration.md`](../app/vault/docs/embedding-calibration.md), not
  a constant change.

## 4. Gated on a written trigger — do not start

- **Chunk-level retrieval** — ADR 0034's evaluation gate.
- **A `ts_headline` arm for previews** — ADR 0031 records why the two obvious
  shortcuts fail and what a real one costs.
- **Batched multi-query search** — only if traces still show repeated-search
  overhead.
- **Batch fetch by id** (`GET /notes?ids=a,b,c`) — ADR 0025; build it when a
  caller needs it, which the librarian's paired-link work may be.
- **Partial HNSW index per profile** and the **dimension-change DDL shape** —
  when a second embedding profile is populated, or a dimension change is
  proposed.
- **Shared storage for the per-principal quota.** Buckets are per process, so
  the effective ceiling is the limit times the worker count. Necessary the
  moment a second dyno exists, not before.

## 5. Deferred decisions — need an ADR before any work

- **Deferred decision #1: whole-vault read permissions.** `folders.yml` governs
  `ai_write` and has no `ai_read`; one `vault:read` reads everything the read
  policy admits. Blocks any `Human/` import. Entangled with **human note
  identity across renames** (ADR 0012 recorded that a human note's id is stable
  only while its row survives); settle both before the first edge points at a
  human note.
- **ADR 0038 — a first-party reviewer authorization.** Recommended deferred:
  the monthly `grant-oauth` step is cheap now that console sessions persist.
  Prefer a narrower `grant-reviewer` convenience if the friction returns.
- **ADR 0041 — human-authored notes in the vault.** Deferred deliberately.
  Revisit when the browse console is in real use and the missing notes are
  felt, or when the dedup gate refuses something a person wrote.
- **ADR 0042 — a mutable state store beside the corpus.** Considered, not
  scheduled. Run the cheap experiment first: a plain structured state file with
  an `attempted_fixes` field, for a week of real use.
- **Making the compile plan binding** — persisted work items and an explicit
  declined state. ADR 0027's amendment names the cost of not doing it.
- **A `vault:export` surface.** The scope and the `snapshot` quota exist; no
  route does. `scripts/export_vault_markdown.py` covers the current need.
- **Extracting `app/vault/` to its own repository.** The enabler for a two-tier
  release; [`vault-extraction-manifest.md`](../app/vault/docs/vault-extraction-manifest.md)
  is the checklist. Trigger: the first consumer that needs only one of the two
  products.
- **`superseded` on review cases** stays reserved, with no path that sets it.

## 6. Leaderboard

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

## 7. Tooling and hygiene

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
