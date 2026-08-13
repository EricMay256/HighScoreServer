# Handoff — corpus imported and synthesized, digest fixed, search contract next

**HSS repo:** `C:\Users\yarom\Code\HighScoreServer\HighScoreServer`
**Worktree:** `.claude\worktrees\vault-embedding-provider-0030a7`
**Branch:** `ai-claude/vault-v1-handoff-077cad`, HEAD `2ce9550`, tree clean.
**Knowledge platform:** `C:\Users\yarom\Code\knowledge-platform`, branch `dev`, HEAD `8df16e0`.

**`59985a4`, `5bdd5ad`, `9aebb5f` and `2ce9550` are unpushed**; `origin/dev` is `0c9fb9f`. CI
has not seen the calibration work, the retry-budget change, the facets column, migration
`0005`, or migration `0006`.

> This file goes stale fast. The durable record is `app/vault/docs/adr/` (0001–0017),
> `app/vault/docs/embedding-calibration.md`, and the "Deferred decisions" section of
> `app/vault/docs/vault-architecture.md`. Treat §1 as an index into those.

---

## 0. Environment

pgvector 0.8.6 in local PostgreSQL 17.9; the vault schema lives in the ordinary dev database
`leaderboard`. `VAULT_DATABASE_URL` unset, so local HSS is the real shared-database topology.

| | |
| --- | --- |
| Server | PostgreSQL 17.9, `localhost:5432` |
| `leaderboard` (dev) | leaderboard `0004_auth_identities`; vault **`0006_request_digest_version`** (head) |
| `leaderboard_test` | both lineages at head (vault `0006_request_digest_version`) |
| Corpus | `vault_documents` holds **48**; the markdown corpus is **50** (two notes written after the import — see 2c). 48 embeddings, 48 audit events, 48 write requests |
| Digest versions | 4 write requests restated to `digest_version` 2 by replay; **44 still at 1** and will restate on next touch |
| Vault credential | principal `importer`, scopes `vault:read vault:write`. **The principal name is load-bearing — see §4** |

The venv is in the **main repo**, not the worktree:

```powershell
& "C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.venv\Scripts\Activate.ps1"
```

**`TEST_DATABASE_URL` in `.env` is still the placeholder** `role:password@...` and fails auth.
`leaderboard_test` now exists and is migrated — point at it explicitly per command:

```bash
TEST_DATABASE_URL="postgresql://postgres:<pw>@localhost:5432/leaderboard_test" python -m pytest
```

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
exactly once, and `check-wiki` is now 0/0/0.

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

1. **Push `59985a4`, `5bdd5ad`, `9aebb5f` and `2ce9550`.** CI has seen none of it.
2. **Remove `VAULT_EMBEDDING_TIMEOUT_SECONDS=10` from `.env`** (and check the Heroku config
   var). It overrides the new 5.0 default; at 10s the worst case is 38s, past the router budget.
2b. ~~Apply `0005_document_facets` to `leaderboard`.~~ **Done 2026-08-13**, along with `0006`.
2c. ~~Re-run the importer.~~ **Done 2026-08-13** — 48 documents, verified. Then drifted again:
   the session wrote two more vault notes (`cb6a42ec`, `f66cd89c`, on the digest defect and the
   duplicate-guard asymmetry), so the markdown corpus is 50. Re-running now inserts those two,
   replays the other 48 cleanly, and restates the remaining 44 digests. **That the corpus
   drifted twice inside one session is the argument for task 8's reconcile mode.**
2d. **Decide the update path, before widening the importer's payload.** There is currently no
   way to *change* a document through the write surface: a replay returns the stored response
   and a conflict refuses, and neither carries new values onto an existing row. So the 48
   imported documents cannot receive `facets`, `summary`, `aliases`, `related_ids` or
   `source_ids` by re-running the importer, however wide its payload gets — and widening it
   changes the digest again for no gain. The fork, recorded in ADR 0016's 2026-08-13 amendment:
   a distinct update endpoint keyed on document id (keeps the write path's semantics clean), or
   an opt-in "replay may update when the body differs" (one endpoint, conditional idempotency).
   **This blocks task 5 and any facet backfill.**
2e. **The markdown authoring schema has no facets.** `Vault/00 Governance/Schemas/` has zero
   matches for facet, and no Agent Note carries `Aliases`, `Summary` or `SourceIDs`.
   `RelatedIDs` is present on all 48 and **non-empty on none**. So there is currently nothing
   to backfill even once 2d exists — the vocabulary decision (tasks 4 and 5) and a schema
   change have to come first, followed by re-annotating 48 notes through the engine.
3. **Search contract alignment** (§2).
4. **Should `tags` be in the embedding text at all?** — an ADR 0013 question, and the single
   thing most constraining whether `flag_at` can ever be calibrated. Tags move the corpus
   *maximum* by ~0.05 while barely moving the mean, against a 0.0094 margin. ADR 0017 moved
   classification out, but `gotcha` (18 notes) and `tooling` (7) will never be facets. Needs
   the counterfactual measured on all 39 documents, not the 14 sampled. Removing tags would
   drop the floor and could open a real gap — at the cost of tags no longer contributing to
   semantic ranking.
5. **Migrate `hss` / `b2-migration` from tags to facets.** They are project names inflating
   dedup today. Changes their embedding text, so it needs a re-embed — a data operation with a
   cost, not a cleanup.
6. **Review surface** — `vault:review` is recognised and granted by no route. Lower priority
   than it looks: at `flag_at = 1.0` the queue only fills on exact resubmission.
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
- **`ruff format --check` is not clean on the repo** — 13 files would be reformatted, most
  untouched for months. `ruff check` *is* clean. Don't reformat wholesale; format only the lines
  you add, or the diff drowns in unrelated churn.
- **PowerShell `Set-Content -Encoding utf8` writes a BOM** on 5.1. A token read back from such a
  file carries three junk bytes into the Authorization header. Decode `utf-8-sig`.
- **Vault Alembic is a separate lineage:** `alembic -c alembic-vault.ini upgrade head`.
- **Never run two pytest processes against `leaderboard_test` at once** (see §0).
- **PostgreSQL rejects subqueries in CHECK constraints.** Migration 0005 works around it with
  an IMMUTABLE SQL function, same shape as `text_array_to_string` in 0004.
- Revision ids must fit `varchar(32)`.
- `ruff target-version` must be the oldest runtime (3.12), never the local interpreter.
- **Never pipe a check you intend to trust** — use `${PIPESTATUS[0]}`.
- Standalone scripts must set the SelectorEventLoop policy themselves on Windows.
- Windows/PowerShell: `curl.exe` not `curl`; `< file` unsupported; `$env:` does not persist.
  The Bash tool is Git Bash — PowerShell here-strings are a syntax error there.
- Shell state does not survive between separate command invocations.
