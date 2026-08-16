# Handoff — code-review findings applied; search contract next

**HSS repo:** `/home/ubuntu/projects/HighScoreServer` (WSL, `Ubuntu-24.04`)
**Worktree:** `.claude/worktrees/vault-readonly-slice-review-0f7ee7`
**Branch:** `ai-claude/chat-findings-option-a-30464c`, tree clean.
**Knowledge platform:** `/home/ubuntu/projects/knowledge-platform`, branch `dev`, HEAD `8df16e0`.

**`origin/dev` is `9eb69ec`, and everything through it is pushed** — the earlier claim that
the calibration, facets, digest, quota, update and retire work was unpushed is obsolete. The
branch above carries four further commits applying a code-review pass (§1c), not yet pushed.
`git log --oneline origin/dev..HEAD` is the authoritative list — a hardcoded one here goes
stale on the next commit.

> This file goes stale fast. The durable record is `app/vault/docs/adr/` (0001–0019),
> `app/vault/docs/embedding-calibration.md`, and the "Deferred decisions" section of
> `app/vault/docs/vault-architecture.md`. Treat §1 as an index into those.
>
> **Companion:** [`HANDOFF-METADATA.md`](HANDOFF-METADATA.md) — the tags / facets / edge-graph
> decisions, with the measured corpus state behind them. Split out because it is a decision
> brief for a fresh session rather than a status report, and because keeping the corpus
> measurements in one file stops two copies drifting apart. Task 4 points at it.

---

## 0. Environment

pgvector 0.8.6 in local PostgreSQL 17.9; the vault schema lives in the ordinary dev database
`leaderboard`. `VAULT_DATABASE_URL` unset, so local HSS is the real shared-database topology.

| | |
| --- | --- |
| Server | PostgreSQL 17.9, `localhost:5432` |
| `leaderboard` (dev) | leaderboard `0004_auth_identities`; vault **`0006_request_digest_version`** (head) |
| `leaderboard_test` | both lineages at head (vault `0006_request_digest_version`) |
| Corpus | `vault_documents` holds **49**; the markdown corpus is **50** (one note written after the last import). 49 embeddings |
| Digest versions | Nearly all restated to `digest_version` 2; stragglers restate on next touch |
| Vault credential | principal `importer`, scopes `vault:read vault:write`. **The principal name is load-bearing — see §4** |

The venv is in the **main repo**, not the worktree:

```bash
source /home/ubuntu/projects/HighScoreServer/.venv/bin/activate
```

**`TEST_DATABASE_URL` is fixed as of 2026-08-14** — it was the `.env.example` placeholder
`role:password@...`, which never worked because **there is no `role` user on this server; the
only superuser is `postgres:postgres`**. It now points at `leaderboard_test` directly, so a
bare `python -m pytest` runs from a worktree with no per-command override (478 passed).

If a run suddenly fails with `FATAL: password authentication failed for user "role"`, that
line has been copied back from `.env.example` — the code under test is not the cause.

**Do not run two pytest processes against it at once.** The autouse `clean_tables` fixture
`TRUNCATE`s, and two concurrent runs deadlock — that produced 26 spurious failures this session.

---

## 1. What this session did

### Calibration is now a per-model procedure, not a constant (`59985a4`)

ADR 0016 justified `flag_at = 1.0` with an ungrounded claim — "unrelated prose routinely
exceeds 0.7". Measured over 741 pairs: p50 0.2542, p99 0.6265, max 0.7406. The claim is false.

**The important finding is the reasoning, not the numbers.** The corpus distribution alone
suggests a wide empty band between 0.7406 and 1.0 where a threshold could safely sit, and the
previous handoff proposed 0.85 on exactly that basis. Measuring the *other* side refutes it:
deliberate restatements score **0.7500, 0.7664, 0.8431** — inside that band. `0.85` catches
none of them. **The band looked empty because nothing had been measured in it.**

So the derivation is two-sided: the corpus gives a floor (what must not flag), reference
duplicates give a ceiling (what must flag), and a threshold is adoptable only if they separate
by more than `MINIMUM_SEPARATION` (0.05). They do not — floor 0.7406, ceiling 0.7500, margin
**0.0094** — so `flag_at` stays 1.0, now measured rather than provisional.

- `app/vault/calibration.py` — reference pairs + pure derivation
- `scripts/measure_dedup_similarity.py` — runs both sides against any configured model
- `app/vault/docs/embedding-calibration.md` — **the durable artifact**; procedure + register
- Every settled write records `top_similarity` in `vault_write_requests.response`, so the
  floor half accumulates from real traffic. Write requests are prunable — harvest before.

A methodology bug was found and fixed mid-session: the two sides were measured on *different
text shapes* (corpus vectors carry title + aliases + tags; reference pairs were bare prose).
Corrected, the margin widened 0.0072 → 0.0094 without changing the verdict.

### Retry budget settled (`59985a4`)

Measured: single query p50 0.163s / p99 1.194s; a 128-document batch 0.728s — *faster* than one
query, since connection setup dominates. The architecture doc pre-registered the rule, so it
applied: **3 attempts at a 5s timeout**, worst case 23s inside Heroku's 30s router budget.

Not academic — two transient failures occurred during the measurements themselves, each
costing the vector arm entirely at one attempt, one aborting a full calibration run.

### Facets (`5bdd5ad`, ADR 0017, migration `0005_document_facets`)

Classification (`{"project": ["hss"]}`) in a JSONB column the embedding assembler never reads.

Why a column and not namespaced tags: `tags` is in the embedding text, and a shared tag raised
**all 45 pairs** in a ten-document sample, mean +0.0385. Against a 0.0094 margin that is over
4x — it would lift the floor above the ceiling and make calibration impossible, not just harder.

The v1 contract now also carries `related_ids`, `source_ids`, `aliases`, and `summary`. All
four already existed in the schema with **zero rows using them**, because the write path could
never set them.

### Two pre-existing test defects fixed

Both would have broken CI. `test_search_returns_lexical_hits` assumed
`VAULT_EMBEDDING_API_KEY` was absent — untrue once a real key landed in `.env`. The 429 test
spent its burst slowly enough for the token bucket to refill, so it passed alone and failed in
a full run.

---

## 1b. Session of 2026-08-13 (librarian pass, import, digest fix)

### The wiki was recompiled — 2 pages to 13 (`8df16e0`, knowledge-platform)

`check-wiki` reported **40 of 48 notes cited by zero pages**; the last compile was 2026-07-10
and covered 8. A full-flush `compile plan --all` run produced 13 pages covering all 48 notes
exactly once. Two more notes were written later in the session and compiled into a 14th page,
so the wiki now stands at **14 pages over 50 notes** and `check-wiki` is 0/0/0.

Two pages were rewritten rather than added: `rag-and-retrieval-design-for-the-b2-engine` and
`unity-package-cache-and-project-initialization`. The threshold material moved out of the RAG
page into a new `semantic-dedup-threshold-calibration`, which reconciles the three notes on the
subject into one argument rather than listing them — the corpus measurement is framed as a
floor only, and "calibrate from the review queue" is explicitly demoted, since a queue never
fills at a safe default threshold.

**The lint key-order workaround is obsolete.** `compile finish` committed first try. Vault note
`7164a912` has said `RESOLVED 2026-07-10` since the serializer fix; the workaround is still
being carried in briefs that predate it.

### The corpus is imported — 39 → 48

Ran `0005_document_facets` against `leaderboard` (it had only ever been applied to
`leaderboard_test`), then the importer. All verification passes: 48 documents all
`status=active`, 48 embeddings under `openai/text-embedding-3-small:1536`, 48 write requests,
`source_sha256` NULL throughout, 0 review cases. **All 48 documents have empty facets.**

### The idempotency digest was broken by additive schema change (`2ce9550`, migration `0006`)

The import surfaced it: 39 of 48 notes came back **409, with byte-identical payloads on the
wire and no note having changed**. The digest hashed the validated model with
`exclude_none=False`, so it covered fields the caller never sent, at their defaults — `5bdd5ad`
adding five optional fields changed the digest of every request that had ever been made.

Generalised: under the old rule *any* additive, backward-compatible field addition invalidates
every idempotency record in the table, and it surfaces at the next replay rather than at the
deploy that caused it.

Fixed by hashing only supplied fields (`exclude_unset=True`) plus
`vault_write_requests.digest_version`, so a stored digest carries the rule that produced it.
Stored digests are **not recomputable** — the payloads were never kept — so a version mismatch
replays without comparing, logs that it did, and then **restates the digest under the current
rule**, so a row is uncomparable for one call rather than permanently. Current-rule keys still
conflict exactly, and both halves are asserted. A version mismatch never overwrites the stored
*document*: that would make retry-after-timeout replace current content with a stale payload,
bypass the dedup gate, and force a re-embed — overwrite belongs in the update operation of 2d.
Verified end to end: the importer now reports `replayed`, not `conflict`.

**The importer was already committed** as `f942917` — item 8 below was stale.

### The update endpoint landed (ADR 0018)

`PUT /api/v1/vault/notes/{note_id}`. See 2d below for the shape and the reasoning. Three
consequences worth carrying forward, all recorded in the ADR:

- **Flagged documents cannot be corrected through it** — they sit outside `READABLE_STATUSES`,
  so an update targeting one is a 404. Adjudication belongs to the unbuilt review surface.
- **Last write wins.** No version column, no `If-Match`. The advisory lock serializes the calls
  but cannot detect the conflict, because a full replacement has no server-side
  read-modify-write span to detect it in. Theoretical at one sequential importer; real the
  moment a second writer exists.
- **It does not touch `source_sha256`,** so human-layer sync must not reuse this endpoint — a
  replica row would be edited out from under its file without ADR 0012's reconciliation
  noticing. Latent today, since every row is NULL.

`READABLE_STATUSES` moved from `routes.py` to `read_policy.py` so the write path can apply the
read rule without importing the transport layer.

### Both surfaces can now update and retire (ADR 0018, ADR 0019)

`PUT /api/v1/vault/notes/{id}` and `DELETE /api/v1/vault/notes/{id}`, with `vault_contrib update`
and `vault_contrib retire` as their markdown counterparts. The engine also reslugs a note's
filename when its title changes, which it previously never did.

Retire is a **delete**, not ADR 0008's archived status. Archived is for content that is
superseded but true; this is for content that is false, where a resolvable row is the failure.
The write-request ledger keeps its row with a null `document_id` — deleting it would let a
replayed key recreate the retired document, turning a retry into an undo.

Used in anger this session: five notes revised, one retired on both surfaces, and one retitled.

**Two gaps this leaves.** The markdown CLI refuses to retire a note a wiki page cites, naming
the pages; the HTTP endpoint has no equivalent because wiki pages are not in the database at
all. And the two surfaces retire independently — there is no deletion path from markdown to the
service, so retiring means doing it on both, deliberately.

### The contribute quota now diverges from the integration spec, on purpose

`contribute` was `10/min burst 3`; it is now **`30/min burst 20`**. That shape assumed
contributions trickle in. They do not — they arrive in batches, and burst 3 throttled every
librarian session and every importer run end to end without touching the abuse case, which is
sustained rate. Long-run exposure is bounded by `per_minute` alone; `burst` only decides how fast
the first few land, so a generous burst against a modest sustained rate costs little.

`test_limits_match_the_integration_spec` no longer covers `contribute`; a separate test asserts
the new numbers and says why, so this reads as a decision rather than drift. **The spec's limits
table should be updated to match, or the divergence accepted explicitly** — see task 12.

The importer's `DEFAULT_DELAY_SECONDS` tracked down 6.0 → 2.0 to match the new sustained rate.
A run of 20 notes or fewer now clears the burst and is not paced at all.

**This does not make concurrent contribution fast, and nothing here changes that.** The vault is
already fully async; the serialization is the corpus-wide `pg_advisory_xact_lock` that ADR 0016
holds across check-dedup-then-write deliberately.

`VAULT_DB_POOL_SIZE` was the *other* serializer and is no longer — it moved 1 → 2 on
2026-08-14, so the surface can serve two callers at once at all. That was a floor, not a
throughput change: a request checks out twice in sequence (authenticate, then serve), so at
size 1 a second concurrent request simply failed on the 5s pool timeout. Queueing now happens
at the lock rather than at the pool, which is the cleaner failure mode at the same write
throughput. See §1c and ADR 0016's 2026-08-14 amendment.

The real lever for batch throughput is still a **batch contribution endpoint** (one lock
acquisition, one transaction, embeddings computed concurrently up front), which needs a
per-item outcome model first.

---

## 1c. Session of 2026-08-14 (code-review findings)

Four commits on `ai-claude/chat-findings-option-a-30464c`, from an external review of
`origin/dev`. Full suite green (478 passed) after each.

### Unauthenticated callers could force database work (`ed34d2d`)

Verifying a credential is a database round trip, and nothing was charged before it. The vault
routes carried no IP-keyed limit, and the host's slowapi `Limiter` has no `default_limits`, so
`SlowAPIMiddleware` covered them with nothing. `parse_token` rejects malformed tokens for free,
but the format is documented and trivially generated.

**The fix is a router-level dependency, and the review's first suggestion — a
`@limiter.limit` decorator on each route — would not have worked.** FastAPI solves dependencies
before calling the endpoint, and authentication *is* a dependency, so a decorator charges after
the round trip it exists to prevent. `tests/vault/test_rate_limit.py` pins the distinction:
five requests against a 3/minute guard reach the credential lookup three times.

slowapi is now a vault dependency, deliberately reversing the note in `rate_limit.py`'s
docstring. It is a *third-party* import, not a host import — `app/vault/` still contains no
`from app.` and `test_boundaries.py` still passes — and the pre-auth layer is the one that
benefits from shared storage, available via `VAULT_RATE_LIMIT_STORAGE_URI`. The per-principal
token bucket stays; the two answer different questions.

### The vault got a second connection, and saturation stopped looking like a bug (`da5177d`)

See the amended paragraph in §1b. The budget passed by exactly one connection, so this was a
two-variable change: HSS 5 → 4 pays for vault 1 → 2, holding at 14 of the 15 available after
the 25% reserve. 5 was never measured — it was the default in `app/db.py`. A test pins that
HSS at 5 no longer fits, so a half-applied config change fails in CI rather than at boot.

**This lowers the HSS pool on merge whether or not the vault is enabled**, and any deployment
setting `HSS_DB_POOL_MAX_SIZE` or `VAULT_DB_POOL_SIZE` explicitly overrides the new defaults
and must be updated together. Check with `heroku config` before enabling the vault.

Separately, an exhausted pool raised `sqlalchemy.exc.TimeoutError` with no handler registered,
so saturation surfaced as a 500 — telling the caller not to retry something transient, and the
error tracker to report a bug. Now 503 with `Retry-After`.

**`HSS_PROCESS_COUNT` is deliberately not wired to `WEB_CONCURRENCY`.** Heroku's Python
buildpack sets that per dyno from CPU and RAM and never as a config var, so `-w
${WEB_CONCURRENCY}` would make the connection budget a function of dyno size: a 1 GB dyno
yields 4 workers, 26 against a ceiling of 15, and the boot aborts. Reasoning is in
`.env.example` next to the formula.

### Authenticated reads stopped costing writes (`60c38ab`)

`touch()` ran an unconditional `UPDATE` on every successful auth, so `get_note` at 120/min
meant 120 writes a minute to one hot row per principal. Now sampled at 60s by predicate, so a
recently-touched row matches nothing and PostgreSQL rewrites nothing.

### ADR 0016 states what its lock costs (`75dea7b`)

Amendment only, no code. The ADR argued why a per-key lock fails but never priced the one it
chose; a reader who noticed the cost first read it as an oversight.

### The embedding timeout was over budget everywhere, and is now bounded

`VAULT_EMBEDDING_TIMEOUT_SECONDS` is **per attempt**, so 10 was never a 10s
ceiling — the budget is `3 x timeout + 2 x 4s backoff`, which is 38s against a 30s router
limit. It was 10 in `.env`, `.env.example` and `vault-configuration.md`.

The guard that should have caught it, `test_worst_case_retry_budget_fits_inside_the_router_timeout`,
computes from `DEFAULT_EMBEDDING_TIMEOUT_SECONDS` — the constant, which was already 5. So it
passed for months while nothing that ran was under 23s. **A test on a default cannot see what a
deployment configures**, and that is the transferable lesson here rather than the specific
number.

Now validated at the environment boundary, with the arithmetic in the message. The retry
constants moved from `embeddings_openai.py` to `constants.py` so `settings.py` can reach them
without importing a transport module — the separation that keeps the Alembic environment free
of httpx. The adapter's `timeout_seconds` parameter stays unbounded on purpose: a batch
backfill has no caller waiting. `scripts/measure_embedding_latency.py` gained `--timeout` for
exactly that reason, since measuring where the ceiling belongs requires exceeding it.

### Not done, deliberately

`_canonical_request_digest` still has **no golden test** — task 14. The existing
`test_the_digest_ignores_fields_the_caller_did_not_send` computes its expectation the same way
the function does, so a field *reorder* passes it while silently changing every stored digest.

---

## 2. Next — track #5, the search contract

**This is the largest remaining piece and should start fresh.** It has three inputs that must
be held at once:

1. **The spec's contract.** `vault.search` is POST with `kinds`/`tags` filters and a
   citation-shaped response carrying `excerpt`, `line_start`, `canonical_url`. Only the *path*
   was ever aligned. Needs snippet extraction and a public base-URL setting. Add `facets` as a
   third filter.
2. **Filters must push down into both arms.** Applied after reciprocal rank fusion they return
   fewer than `limit` and corrupt the ranking, because RRF ranks over each arm's candidate set.
3. **pgvector's HNSW post-filters.** It retrieves by distance and *then* applies `WHERE`, so a
   restrictive facet filter can return far fewer rows than requested — or none — while matches
   exist. Raise `hnsw.ef_search`, over-fetch, or use partial indexes. Not a bug to fix here.

**A design question the spec does not answer:** search-with-filters and browse-by-filter are
different operations. "Show me everything tagged `unity`" has no query to rank by. Decide
whether `q` becomes optional or a separate list endpoint appears. A tag-census endpoint
(`GROUP BY unnest(tags)`) is trivial and is probably the real grouping surface.

Index shapes are in place: GIN `text[]` for `tags` (`&&`, `@>`), GIN `jsonb_path_ops` for
`facets` (`@>` only — no existence operators).

---

## 3. Remaining tasks

1. ~~Push `59985a4`, `5bdd5ad`, `9aebb5f` and `2ce9550`.~~ **Done** — `origin/dev` is `9eb69ec`.
   The four commits of §1c are the only unpushed work.
2. ~~Remove `VAULT_EMBEDDING_TIMEOUT_SECONDS=10` from `.env`.~~ **Done 2026-08-14**, and the
   hole behind it closed. It was 10 in `.env`, `.env.example`, and `vault-configuration.md`;
   all three are 5. `EmbeddingSettings.from_environment` now rejects any configured timeout
   whose full budget exceeds 30s (max 7.3s), printing the arithmetic — the retry constants
   moved to `constants.py` so settings can check them without importing a transport module.
   Not a deploy risk: the variable is **unset on Heroku**, and the validation only runs when
   the vault is enabled, which it has never been.
2b. **Nothing added since the last merge to `main` is configured on Heroku** — 23 variables
   across the connection budget, Steam auth, and the whole vault. Confirmed 2026-08-14. That
   is safe rather than pending work: `REQUIRED_ENV_VARS` is unchanged, so every one of them
   has a default and a merge deploys without a config change. Two consequences to carry:
   **the HSS pool silently drops from 10 per worker to 4** (`main` hardcodes `max_size=10`;
   it is now configurable, default 4 — a fix, since 10 × 2 workers allocated the entire
   20-connection limit, but a real reduction in concurrency), and **Steam endpoints stay
   unavailable** until their three variables are set. The vault ships dark and is meant to.
   Written up in README "Deployment" and banner-noted at the top of `vault-configuration.md`.
2b. ~~Apply `0005_document_facets` to `leaderboard`.~~ **Done 2026-08-13**, along with `0006`.
2c. ~~Re-run the importer.~~ **Done 2026-08-13** — 48 documents, verified. Then drifted again:
   the session wrote two more vault notes (`cb6a42ec`, `f66cd89c`, on the digest defect and the
   duplicate-guard asymmetry), so the markdown corpus is 50. Re-running now inserts those two,
   replays the other 48 cleanly, and restates the remaining 44 digests. **That the corpus
   drifted twice inside one session is the argument for task 8's reconcile mode.**
2d. ~~Decide the update path.~~ **Done — `PUT /api/v1/vault/notes/{note_id}`, ADR 0018.** Full
   replacement, no idempotency key (a replacement is idempotent by construction), the same dedup
   gate with the document itself excluded, and a **409 that writes nothing** on collision rather
   than flagging — flagging would take an active document out of the read surface as a side
   effect of an edit. Embedding is conditional on `embedded_text_sha256`, so a facets-only edit
   costs no embedding call, which is exactly the backfill's shape.
2e. **The backfill is now blocked on data, not on mechanism.** `Vault/00 Governance/Schemas/`
   has zero matches for facet, and no Agent Note carries `Aliases`, `Summary` or `SourceIDs`.
   `RelatedIDs` is present on all 50 notes and **non-empty on none**. 2d built the endpoint; there is
   still nothing to send through it. Order: the vocabulary decision (tasks 4 and 5), then an
   authoring-schema change so notes can carry facets, then re-annotating the corpus through the
   engine, then teaching the importer to PUT. **Nothing before that step is worth building.**
3. **Search contract alignment** (§2).
4. **Tags, facets, and the edge graph — see [`HANDOFF-METADATA.md`](HANDOFF-METADATA.md).**
   Six entangled decisions with the measured corpus state behind them, kept in one place so the
   numbers do not drift across two files. Headline: `tags` is the *only* metadata the corpus
   carries (70 distinct over 50 notes, 40 of them singletons); `facets`, `related_ids`,
   `source_ids`, `aliases` and `summary` are empty on all 48 rows. The first move is one
   script run — the tag counterfactual on all 50 notes — because every other cost depends on it.
5. **The wiki layer holds a real edge graph that nothing can query.** 50 `SourceIDs` edges
   partition the corpus exactly, plus 21 page-to-page `Related` edges — and `Related` is keyed
   by *title*, not id, which is the one referential inconsistency worth fixing before any of it
   is projected. Detail in `HANDOFF-METADATA.md` §4.
6. **Review surface** — `vault:review` is recognised and granted by no route. Lower priority
   than it looks: at `flag_at = 1.0` the queue only fills on exact resubmission — and after the
   2026-08-15 calibration it is worth restating that the bands now *overlap*, so nothing but an
   exact resubmission will ever reach that queue on this model.
6b. ~~Split `vault:write` into contribute / update / delete.~~ **Done 2026-08-15** — vault ADR
   0020, migration `0007_write_scope_split`. `vault:write` narrowed to contribute; `vault:update`
   and `vault:delete` gate replacement and retirement. **The migration grants nothing** — it
   widens the CHECK constraint and stops there, because a migration reruns and one that
   re-applies privilege silently restores permissions on every rebuild, rollback or staging
   refresh. Widening an existing credential is a manual per-credential `UPDATE`, or a reissue.
   The three local `importer` credentials already hold all four scopes and were left as they
   are; **whether they should keep `vault:delete` is an open call** — the ADR's own example of
   the shape this split exists for is contribute + replace and never delete, but the importer
   lives in the knowledge-platform repo and was not inspected. Credentials remain non-expiring
   by default, decided deliberately: revocation is immediate and needs no cache to expire,
   whereas a lapsed expiry is an outage.
   **The vault lineage head is now `0007_write_scope_split`** — an existing database needs
   `alembic -c alembic-vault.ini upgrade head` before this code runs against it, or every
   credential write fails the old CHECK constraint.
7. **Port `resolve_context`** to close the `folders.yml` ↔ `READABLE_PATH_PREFIXES`
   duplication. Entangled with deferred decision #2 (where governance YAML lives at runtime).
8. ~~Commit the importer.~~ **Already tracked** as `f942917` (2026-08-12). Still worth renaming
   away from `import_to_vault_service` — it is a replay/sync tool, not a one-shot migration —
   and giving it a no-write reconcile mode that reports corpus-vs-database drift. Do that after
   2d, not before.
8b. **Wiki pages are not in the database and cannot be.** `vault_document_kind` reserves
   `wiki`, and `vault_documents_compile_provenance_consistent` requires `compile_run_id`,
   `compiled_by` and `compiled_at` to be NOT NULL for `kind='wiki'` — so a wiki row cannot exist
   without a `vault_compile_runs` row, and that table is empty. The importer walks
   `Vault/Agent/notes/` only. Search therefore returns raw notes and no synthesis. The
   architecture doc calls the intended mechanism a "compiled read-only projection"; the engine's
   compile run id (e.g. `run_20260813_184935`) is what would become the `vault_compile_runs`
   row. **This is a third sync path**, distinct from agent contributions and ADR 0012's
   mark-and-sweep.
9. **More reference pairs.** Three is thin, and the ceiling is a minimum over them.
10. **Human-layer sync** — the other half of ADR 0012. Nothing built.
11. **Export/snapshot** (`vault:export`), **E501**, **UP042**.
12. **Reconcile the integration spec's limits table with `contribute` = 30/min burst 20**, or
   record the divergence in the spec deliberately. The code and its test already state the
   reasoning; the spec is the copy that is now wrong.
13. **A batch contribution endpoint**, if batch throughput ever matters. One request carrying N
   notes, one advisory-lock acquisition, one transaction, embeddings computed concurrently
   before it opens. Blocked on a per-item outcome model — what a 200 means when note 7 of 20
   flags — and on how idempotency keys work for a batch.
14. ~~Pin `_canonical_request_digest` to a golden hex digest.~~ **Done 2026-08-14.**
   `test_the_digest_rule_is_pinned_to_a_golden_value` freezes a fully-populated payload against
   both `REQUEST_DIGEST_VERSION` and the digest hex. Verified by mutation, not by assertion:
   swapping two fields' declaration order breaks it, adding an unsupplied optional field does
   not. Both constants are pinned so that "fix the failing test" cannot mean pasting the new
   hash — the version has to be bumped and a migration written first.
15. ~~Make auth and the handler share one connection checkout.~~ **Rejected 2026-08-14 — do not
   revive without re-reading this.** It appears to halve pool pressure, and does for `get_note`
   and `retire`. But `search`, `contribute` and `update` all call the embedding provider
   *between* their checkouts, deliberately (`service.py`, and vault ADR 0016). A dependency
   that `yield`s a connection holds it for the whole request, so it would pin one across an
   embedding budget of 3 attempts × 5s plus backoff — **up to 23 seconds**. At pool size 2,
   two concurrent searches would exhaust the pool and turn the new 503 into a routine response.
   The original motivation has also shrunk: `touch()` is sampled now, so authentication is
   usually one indexed `SELECT`. Revisit only if `VaultPoolObserver` reports real contention,
   and the answer then is a larger pool, not a longer-held connection.
16. **Decide whether this file and `HANDOFF-METADATA.md` belong in a public repo at all.**
   Moved to `docs/` on 2026-08-14, which fixes the root-level signal but not the contents:
   both still name the private `knowledge-platform` repository and give its checkout path.
   The paths were rewritten to their WSL locations on 2026-08-15, which removed the Windows
   username but not the disclosure. Untracking them is a separate call.

**Blocked / out of scope until re-approved:** MCP (`mcp` is not an approved dependency);
`VAULT_ENABLED=true` in production; partial HNSW index per profile; dimension-change DDL.

**Deferred decision #1 (whole-vault read permissions) still blocks any human-layer import.**
`folders.yml` governs `ai_write` and has no `ai_read`; one `vault:read` scope reads everything
including `Human/07 People/**`.

---

## 4. Environment gotchas

- **Run the importer only as principal `importer`.** `vault_write_requests` is keyed
  `(principal_id, idempotency_key)`, and `vault_documents` has **no** natural-key uniqueness on
  the note id — `id` is a service-minted surrogate and `vault_path` is derived from it, so a
  duplicate import collides with nothing. That ledger is the *only* duplicate guard, and a
  different principal bypasses it silently, writing the whole corpus a second time.
  `issue_vault_credential.py --name <x>` sets the principal, so `--name importer` is required;
  the token secret itself is irrelevant to idempotency. (Human-layer sync will not have this
  problem: ADR 0012 keys identity on `vault_path`, which is a database constraint no credential
  can defeat. The asymmetry is structural.)
- **`VAULT_API_TOKEN` is undocumented.** It appears in no `.env.example`, ADR, or doc — the only
  definition is `DEFAULT_TOKEN_ENV` in the importer. Tokens are shown once and stored as SHA-256
  only, so a lost one means issuing another. Two stale `importer` credentials are unrevoked.
- **`vault_migrations/env.py` hardcodes `Path(__file__).parents[1] / ".env"`**, which does not
  exist in a worktree. Pass `DATABASE_URL` explicitly for the vault lineage. `app/env.py` uses
  `find_dotenv(usecwd=True)` and walks up to the main repo, so the app and `scripts/*` are fine.
- **Neither `ruff format --check` nor `ruff check` is clean on the repo.** 13 files would be
  reformatted, and `ruff check` reports 9 `I001` import-sort errors (2026-08-15) across
  `migrations/env.py`, the four `migrations/versions/*`, `run_dev.py` and `wsgi.py` — all
  auto-fixable, all in files untouched for months. The earlier claim that `ruff check` was clean
  is obsolete. Don't reformat wholesale; format only the lines you add, or the diff drowns in
  unrelated churn. `app/`, `scripts/` and `tests/` are clean, so scope a check to those.
- **PowerShell `Set-Content -Encoding utf8` writes a BOM** on 5.1. A token read back from such a
  file carries three junk bytes into the Authorization header. Decode `utf-8-sig`.
- **Vault Alembic is a separate lineage:** `alembic -c alembic-vault.ini upgrade head`.
- **Never run two pytest processes against `leaderboard_test` at once** (see §0).
- **PostgreSQL rejects subqueries in CHECK constraints.** Migration 0005 works around it with
  an IMMUTABLE SQL function, same shape as `text_array_to_string` in 0004.
- Revision ids must fit `varchar(32)`.
- `ruff target-version` must be the oldest runtime (3.12), never the local interpreter.
- **Never pipe a check you intend to trust** — use `${PIPESTATUS[0]}`.
- Standalone scripts must set the SelectorEventLoop policy themselves on Windows. The
  `sys.platform == "win32"` guards are no-ops under WSL — leave them in place.
- **Check the platform before choosing shell syntax** (`uname -s`); as of 2026-08-15 the
  primary environment is WSL2 `Ubuntu-24.04`, with Windows still supported. See the dev
  environment bullets in `AGENTS.md` for both sets of constraints. Under WSL the shell is
  ordinary bash; on Windows it is PowerShell (`curl.exe` not `curl`, no `< file`, `$env:`
  does not persist).
- Shell state does not survive between separate command invocations.
