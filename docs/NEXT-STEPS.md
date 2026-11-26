# Next steps

Current as of 2026-11-25 for the vault; the leaderboard sections were last revised 2026-09-12. One ordered list of what remains, across the
leaderboard and the vault, with the *why now* and the blocking relationships.
Detail lives elsewhere and is linked; this file exists so nothing else has to
be read to know what to pick up.

Where the durable records are: the two ADR indexes ([leaderboard](adr/README.md),
[vault](../app/vault/docs/adr/README.md)), the
[vault runbook](../app/vault/docs/vault-configuration.md), [`specs.md`](specs.md)
for validated runs, and [`HANDOFF.md`](HANDOFF.md) for the start-here context.
Historical handoffs are under [`archive/`](archive/README.md).

## State

- **`main` is level with `dev` as of PR #35** (2026-11-26, squash-merged as
  `697df70`), so `main` is not an ancestor of `dev` and the next merge is
  another PR. Production runs it, with vault migration 0021 applied by the
  release phase.
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

## Vault direction — decided 2026-11-25

**What the vault is for:** improving the quality of the user's agents —
persistent memory and information across sessions and providers, judged by the
value it returns per token of context it costs. Not by corpus size, and not by
governance completeness. The vault is deliberately larger than the leaderboard
and built for a corpus that has not arrived yet; that is accepted.

The 2026-11-25 review found effort concentrated on *write correctness* and
almost none on whether notes are *read, used, and kept true*. What follows moves
it. The decisions are vault ADRs 0053–0062 and amendments to 0016, 0023, 0025,
0030–0032, 0034, 0043 and 0048:

- **The librarian is replaced** by client hooks, existing tools and scheduled
  routines — vault ADR 0053, which rejects ADR 0043 (§2).
- **Human vault:** phase B stands as built; the Obsidian extension is not built;
  offline access is a read-only download — ADR 0054 (§4).
- **The operator side is frozen** — OAuth, both login methods, labels,
  entitlements, the consoles. Its intended use, one credential per provider
  with the narrowest scopes, is already supported.
- **Retrieval tuning is frozen until 500 active documents** — ADR 0055 (§5).
- **Extraction moves up**, with a deployment-shaped definition of done — ADR
  0062 (§6).
- **`app/vault/AGENTS.md` was cut** from 832 lines to 225 on 2026-11-25; the
  full prior text is kept verbatim in `app/vault/docs/invariants-rationale.md`.

## 1. Immediate

1. **Bring `main` level with `dev`. Done 2026-11-26: PR #35, squash-merged as
   `697df70`.**
   - **Deploy:** Heroku deployed it successfully. The release phase exited
     cleanly, and it runs the vault `upgrade head`, so migration 0021 is
     applied.
   - **Verified live:** the leaderboard, SPA and docs; the vault's docs, OAuth
     metadata, landing page, 401s and MCP 307; no vault paths in HSS's OpenAPI;
     no CORS on vault routes; an authenticated MCP search through the claude.ai
     connector.
   - **Still unchecked directly:**
     `heroku run "python -m alembic -c alembic-vault.ini current"`. The CLI
     token had expired.
2. **Settle the production game-mode list.** Nothing hardcodes a mode any
   more, so `/leaderboard` and the SPA both land on the first row of
   `/game_modes`, ordered by `name` — the alphabetically first configured mode
   *is* the landing page. `blitz` is expected to sort first and unused modes
   are to be pruned, but neither has been confirmed against the deployment.
   The failure is silent: a stale or test mode sorting earlier becomes the
   front page and nothing errors.
3. **Backfill note summaries.** 67 of 80 production notes lacked one on
   2026-08-28. `summary` joins the embedding text and is the search preview
   (ADR 0031), so an unsummarized note is measurably harder to find.
   `scripts/backfill_vault_summaries.py` plans and applies; an agent writes the
   prose. A natural first job for the garden routine (§2).
4. **Decide whether the four largest notes want splitting.** 10.7k–15.4k
   characters against a corpus mean of 2.1k. Editorial before technical: is
   each one insight or several?

## 2. The agent loop — vault ADR 0053

**Phase 1. Client side, in knowledge-platform; no HSS change.**

- **Package `knowledge-vault` as a Claude Code plugin** — skill, hooks and hook
  scripts in one unit, from a private marketplace. Today the skill reaches
  only the projects `sync_skill.py` copies it into. On the Windows machine it
  was absent while the MCP connector was present. The MCP server stays a
  claude.ai connector, which already syncs per account and works in Orca and
  cloud sessions. Still unverified: whether a plugin synced through the
  claude.ai account carries hooks as well as skills. If not, each device runs
  `/plugin marketplace add` once, plus `gh auth setup-git` for auto-update.
- **Four hook behaviours**, specified provider-neutrally in ADR 0053 so Codex
  and others can implement their own equivalents: retrieve on failure, reflect
  at stop after substantial work, inject promoted titles at session start, and
  mark a reflection due before compaction.
- **Skill guidance:** write on resolution, not on failure; amend rather than
  add; read the activity log before gardening or reflecting; file synthesized
  answers back through `source_ids`; follow the answer-grounding protocol
  ([`answer-grounding-now-spec.md`](../app/vault/docs/answer-grounding-now-spec.md)),
  which is client guidance with an offline evaluator and already fits this
  shape.
- **The garden routine**, run on demand or on a schedule with an action budget.
  Its credential holds `vault:read`, `vault:write`, `vault:propose` and
  `vault:compile`, and nothing more.
- **Measure for a week:** contributions per session, flagged outcomes,
  contributions at or above 0.75 similarity, and reflections that ended in
  "nothing durable".

ADR 0044 (proposal lineage) is independent of all this and stays Accepted but
unscheduled.

## 3. Corpus quality and retrieval

**Phase 2. HSS, code only.**

1. **Refuse tool-call markup** (`</summary>`, `<parameter name=`, and similar)
   in title, summary and tags at the write boundary. Note
   `8b01449717c14ec5a91206c2ffcfeacb` stored a leaked tool call in its summary,
   and its tags are probably inside it. Repair it by amendment.
2. **The deterministic health report** — vault ADR 0056. A script first, then a
   read-only MCP tool. It absorbs the tag census.
3. **The similarity warning** — ADR 0032's 2026-11-25 amendment.
   `similarity_warning_at` defaults to 0.75 and is advice only. Check it
   against the `test_mcp_budget.py` ratchet.
4. **Recalibrate `flag_at`** — ADR 0016's 2026-11-25 amendment. Build 15–20
   realistic duplicate pairs, then choose an operating point under the new
   cost-trade rule and record it in `embedding-calibration.md`. Review cases
   become reachable once `flag_at` drops below 1.0.
5. **The activity log** — vault ADR 0057: `vault_recent_activity` plus an
   exported `Agent/_log.md`.

**Phase 3. Each item is a vault Alembic revision.**

6. **Usage counters** — vault ADR 0058.
7. **Feedback on notes** — vault ADR 0059.
8. **`supersedes_ids`** — vault ADR 0060.
9. **Promotion nomination and the new meaning of `promoted`** — vault ADR 0061.
   Includes `vault_list_promoted`, which the session-start hook consumes.
10. **Link evolution** — near neighbours returned as paired-link suggestions
    (A-MEM; vault note `20aabea1…`).

Still open from before:

- **Validate the retrieval label set.** Every case in
  `app/vault/retrieval_cases.py` is `validated=False`. A human pass turns
  `scripts/measure_retrieval_quality.py` into a regression signal, and the
  500-document unfreeze (§5) is measured against it.
- **Exporter versus linter key order.** Lower priority now that no
  bidirectional sync is planned; it matters only to the offline download.
- **Facet vocabulary.** Half the corpus is tagged `gotcha`, a kind rather than
  a topic. Decide a `kind` axis on queryability alone, and re-embed affected
  notes in one pass.

## 4. The Human vault

Vault ADR 0048 selected database authority for enrolled Human notes. As of
2026-11-25, vault ADR 0054 partially supersedes it. **The Obsidian extension is
not built, and offline access is a read-only download** containing exactly what
the downloading credential could read. That download is the `vault:export`
surface, whose scope and `snapshot` quota already exist. Until it is built,
`scripts/export_vault_markdown.py` covers it.

- **A — baseline and export rehearsal. Done locally 2026-09-12**, except
  deployment parity. See the
  [handoff](../app/vault/docs/human-vault-handoff.md#phase-a-result-2026-09-12).
- **B — the Human service boundary. B1–B4 built** on `dev` (vault ADRs
  0049–0052, migration 0021): the collection boundary, audience-aware reads,
  create/edit/move, and tombstoned deletion with the change feed.
  **B5, a disclosure regression suite across every Agent surface, remains** —
  and it matters more now that Human rows exist in the schema.
- **C — browser authoring of Human notes. To build** (decided 2026-11-25): its own
  OAuth family, explicit save and conflict states, a separately granted delete, and
  visible read-policy and semantic-index state. This is the web interface the
  2026-11-25 direction relies on. B5 comes first.
- **D — the Obsidian extension. Dropped** (ADR 0054).
- **E — daily Human indexing. To build** (decided 2026-11-25), per the
  [daily embedding specification](../app/vault/docs/human-embedding-refresh.md).
  It may be built alongside C.

Re-run `scripts/check_read_policy_parity.py` whenever `folders.yml` or
`read_policy.py` changes.

## 5. Gated on a written trigger — do not start

- **Retrieval tuning — frozen until 500 active readable documents** (vault ADR
  0055). Frozen: RRF parameters, the disjunction rule, the snippet bound, a
  `ts_headline` arm, chunk-level retrieval (ADR 0034), batched multi-query
  search, and batch fetch by id. Not frozen: bug fixes, the §3 dedup work
  (write path), and validating the label set. At 500, validate the label set
  first, then re-run `measure_retrieval_quality.py` and
  `measure_chunk_eligibility.py` before changing anything.
- **Partial HNSW index per profile** and the **dimension-change DDL shape** —
  when a second embedding profile is populated, or a dimension change is
  proposed.
- **Shared storage for the per-principal quota.** Buckets are per process, so
  the effective ceiling is the limit times the worker count. Necessary the
  moment a second dyno exists, not before.

## 6. Extraction — moved up (vault ADR 0062)

**Done when:** both products are hosted on the one Heroku app as an ongoing
arrangement, and a Heroku rebuild deploys the current head of `main` from both
repositories.

Decided 2026-11-25:

- **The runtime moves to a new private repository, `knowledge-vault`** (package
  `knowledge_vault`), which holds no corpus.
- **The Heroku app `high-score-server` is re-pointed to it.** At build it fetches
  HSS's public `main`, which needs no credential, and logs both commit SHAs.
- **HSS and the vault are two applications behind a path dispatcher** (host
  ADR 0016). `knowledge-vault` owns `composed.py`, the `wsgi`, the Procfile and
  the release script, and its CI tests the composition against HSS's public
  `main`.

[`vault-extraction-manifest.md`](../app/vault/docs/vault-extraction-manifest.md)
is the checklist. In order:

1. §1.1 — `main` level with `dev`, and the production vault head known.
2. **In HSS, before anything moves: two applications. Implemented on `dev`
   2026-11-25 (host ADR 0016); the database suite has not yet run against it.**
   `create_vault_app()` builds the vault alone, `composed.py` dispatches by
   path, and `app/main.py` names no vault code. Verified locally without a
   database: a lint pass, 35 database-free tests, and a mocked-lifespan smoke
   run of the composed app. Still to verify: the full suite, which needs
   Postgres with pgvector. CI runs only for `main`, and this Windows machine
   has no database.
3. **No staging spike** (operator's choice, 2026-11-25). The build mechanism is
   proven at cutover, under ADR 0062's rollback plan: cut over only when
   production's migration heads already match, keep the vault in HSS until the
   composed app is verified, and use `heroku rollback` if anything fails.
4. **Create `knowledge-vault`. Done 2026-11-25.** It is a private repository,
   extracted with `git filter-repo` from `dev` with its full history (313
   commits): `app/vault/` became `knowledge_vault/`, imports were repointed,
   `hss_path.py` locates the HSS checkout, and it has its own test harness and
   CI composing HSS's public `main`.
   - **Results:** 1343 tests pass locally, against a schema built from empty
     across both repos.
   - **Found on the way:** HSS resolved `public/` and `templates/` against the
     working directory. That is now fixed (`b002a50`, `tests/test_app_paths.py`).
   - **From here, vault changes go to `knowledge-vault`.** HSS's copy is frozen
     for rollback.
5. **Cut over:** re-point `high-score-server` to `knowledge-vault`, deploy, and
   verify. Then remove the vault from HSS per the manifest's "HSS files that
   need editing".

An HSS-only change reaches production on the next build of the vault app. Add a
rebuild workflow later if that lag matters.

## 7. Other deferred decisions

- **ADR 0038 — a first-party reviewer authorization.** Deferred, and covered by
  the operator freeze.
- **ADR 0042 — a mutable state store beside the corpus.** Considered, not
  scheduled. Run the cheap experiment first: a plain structured state file with
  an `attempted_fixes` field, for a week of real use.
- **Making the compile plan binding** — persisted work items and an explicit
  declined state. ADR 0027's amendment names the cost of not doing it.
- **`superseded` on review cases** stays reserved, with no path that sets it.

## 8. Leaderboard

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

## 9. Tooling and hygiene

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
