# Handoff — calibration settled, facets landed, search contract next

**HSS repo:** `C:\Users\yarom\Code\HighScoreServer\HighScoreServer`
**Worktree:** `.claude\worktrees\vault-embedding-provider-0030a7`
**Branch:** `ai-claude/vault-v1-handoff-077cad`, HEAD `5bdd5ad`, tree clean.
**Knowledge platform:** `C:\Users\yarom\Code\knowledge-platform`

**`59985a4` and `5bdd5ad` are unpushed**; `origin/dev` is `0c9fb9f`. CI has not seen the
calibration work, the retry-budget change, the facets column, or migration `0005`.

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
| `leaderboard` | leaderboard `0004_auth_identities`; vault `0005_document_facets` |
| `leaderboard_test` | **created this session**, both lineages at head |
| Corpus | **39** documents, all embedded under `openai/text-embedding-3-small:1536` |
| Vault credential | principal `importer`, scopes `vault:read vault:write` |

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

1. **Push `59985a4` and `5bdd5ad`.** CI has seen none of it.
2. **Remove `VAULT_EMBEDDING_TIMEOUT_SECONDS=10` from `.env`** (and check the Heroku config
   var). It overrides the new 5.0 default; at 10s the worst case is 38s, past the router budget.
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
8. **Commit the importer** to knowledge-platform, or decide against it deliberately. Untracked
   at `engine/scripts/import_to_vault_service.py`, and in practice the sync path until ADR
   0012's reconciliation exists.
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

- **`vault_migrations/env.py` hardcodes `Path(__file__).parents[1] / ".env"`**, which does not
  exist in a worktree. Pass `DATABASE_URL` explicitly for the vault lineage. `app/env.py` uses
  `find_dotenv(usecwd=True)` and walks up to the main repo, so the app and `scripts/*` are fine.
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
