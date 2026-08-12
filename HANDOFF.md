# Handoff — Vault write path complete, importer next

**Repo:** `C:\Users\yarom\Code\HighScoreServer\HighScoreServer`
**Worktree:** `.claude\worktrees\vault-readonly-slice-review-839076`
**Branch:** `ai-claude/vault-readonly-slice-review-839076`, HEAD `12d91bd`
**State:** 409 tests pass, ruff clean, tree clean. **`12d91bd` is unpushed**; `origin/dev` is
`49eede7` and green. PR #10 is open.

> This file goes stale fast — the previous one claimed unpushed commits and unseen CI within an
> hour of being written. The durable record is `app/vault/docs/adr/` (0001–0016) and the
> "Deferred decisions" section of `app/vault/docs/vault-architecture.md`; both are maintained.
> Treat §1 here as an index into those, and re-derive anything load-bearing from git.

---

## 0. Orientation

```bash
cd "C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.claude\worktrees\vault-readonly-slice-review-839076"
git log --oneline -3      # 12d91bd, 49eede7, 5bd478d
git status --porcelain    # empty
```

**The virtualenv is in the MAIN repo**, not the worktree:
`C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.venv\Scripts\python.exe`.

**Local PostgreSQL has no pgvector.** Use the pinned container; a stopped Docker daemon presents
as a *hang*, not an error, so run `docker ps` first if the suite freezes.

```bash
docker start hss-vault-verify   # or `docker run` per app/vault/docs/vault-configuration.md
```

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:55432/highscore_test" VAULT_TEXT_SEARCH_CONFIG=english
python -m alembic upgrade head
python -m alembic -c alembic-vault.ini upgrade head
```

```bash
export TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:55432/highscore_test" VAULT_ENABLED=true HSS_DB_POOL_MAX_SIZE=5 HSS_PROCESS_COUNT=2 DATABASE_CONNECTION_LIMIT=20 DB_OPERATIONAL_CONNECTION_RESERVE=2 VAULT_DB_POOL_SIZE=1 VAULT_DB_POOL_TIMEOUT_SECONDS=5 VAULT_TEXT_SEARCH_CONFIG=english API_KEY=ci-test-api-key JWT_SECRET=ci-test-jwt-secret
python -m pytest -q          # baseline 409
python -m ruff check app/ tests/ scripts/ vault_migrations/
```

A clean full run leaves **zero** rows in the vault schema. Non-zero means a test died mid-way;
sweep `contributed_by LIKE 'agent:test-principal-%'` before trusting dedup-sensitive results.

---

## 1. What exists now

The vault is a working read **and** write surface behind agent credentials, rate limits, and a
path-scoped read policy. Sixteen ADRs; four vault migrations.

| Area | State |
| ---- | ----- |
| Read surface | `GET /api/v1/vault/search`, `GET /api/v1/vault/notes/{note_id}` |
| Write surface | `POST /api/v1/vault/contributions` — validate, embed, dedup, decide, write |
| Auth | `hssv1_<id>_<secret>` against `vault_agent_credentials`; `scripts/issue_vault_credential.py` |
| Rate limits | Per principal, vault-local token bucket, spec's numbers |
| Read policy | `ai_read` from `folders.yml`, enforced at import scope *and* query time, fails closed |
| Schema | `vault_path`, `doc_type`, `doc_status`, `aliases`, `frontmatter`, `source_sha256`, `embedded_text_sha256` |
| Governance | `decide()`/`Policy` ported verbatim (ADR 0004); `folders.yml` carries `ai_read` (applied, `3e16229`) |

**Do not re-litigate** ADRs 0001–0016. The ones most likely to be second-guessed:

- **`flag_at = 1.0`, not 0.85** (ADR 0016). Stage A's number is a *title string ratio*; here the
  score is *cosine similarity*, where unrelated prose exceeds 0.7. Only an identical embedding
  flags, until a threshold is measured. A test pins this.
- **No `policy_scope` column** (ADR 0010). Governance context is a fold over every matching rule,
  not one winner.
- **`READABLE_PATH_PREFIXES` enumerates Agent subfolders** — no blanket `Agent/`. `folders.yml`
  has no `Agent/**` catch-all, so a broad prefix fails open where governance fails closed.
- **No dedup, no write** (ADR 0016). Missing embedding provider is 503, never a silent insert.

---

## 2. Next step — the importer

A knowledge-platform script that walks `Vault/Agent/notes/` and POSTs each note to
`/api/v1/vault/contributions`. Chosen (by the operator) so that migrating the corpus exercises
the real write path rather than a parallel one.

**Scope is the Agent layer only.** Those 36 notes were created through `validate → dedup → decide`
originally, so replaying them is faithful. Human-layer sync is a *different* operation — identity
from the path, unchanged file is a no-op, dedup actively wrong — and is ADR 0012's mark-and-sweep,
not this endpoint. Do not conflate them.

What it needs:

1. **A `vault:write` credential**: `python -m scripts.issue_vault_credential issue --name importer --scopes vault:read vault:write`
2. **A target**. Undecided: a local `python run_dev.py` or a deployed HSS. Local is the obvious
   first run; note that `VAULT_ENABLED` must be true and an embedding provider configured, or
   every contribution returns 503 by design.
3. **The note's existing `ID` as `--run-id`/`idempotency_key`**, so a re-run is a no-op rather
   than 36 duplicates. This is the single most important detail in the script.
4. **Status reporting**: `inserted` / `flagged` / `409` per note. Anything `flagged` under
   `flag_at = 1.0` means byte-identical content and is worth looking at rather than ignoring.

Two things the importer will surface that are not bugs:

- Contributed notes get a **new** id and `vault_path` (`Agent/notes/<uuid>.md`); the service owns
  identity. The original `ID` survives only as the idempotency key. If preserving original ids
  matters, that is a change to the write path, not to the importer.
- The v1 contract carries title/body/tags/source_url only. Frontmatter the Agent notes hold
  (`SourceIDs`, `ClientRunID`, …) is **not** transmitted. Decide whether that matters before
  running it against the real corpus.

**After the import, the calibration becomes possible**: compute the pairwise cosine distribution
over the imported corpus and set `flag_at` from data. That is the intended sequel and the reason
`1.0` is not a permanent answer.

---

## 3. Remaining identified tasks

Ordered roughly by how much they block.

1. **Push `12d91bd`.** CI has not seen the write path.
2. **Calibrate `flag_at`** from the imported corpus (see §2).
3. **Measure embedding latency** — `scripts/measure_embedding_latency.py` is written and
   stub-verified but has never run against the real API; `VAULT_EMBEDDING_API_KEY` is commented
   out in `.env`. Settles the retry budget (`_MAX_ATTEMPTS = 1`), still provisional.
4. **Human-layer sync** — the other half of ADR 0012. Needs a reconciliation entry point and a
   decision on where it runs. Staleness and deletion are designed; nothing is built.
5. **Port `resolve_context`** to close the `folders.yml` ↔ `READABLE_PATH_PREFIXES` duplication.
   The manual diff found a real fail-open divergence on its first run, so this is not theoretical.
   Requires copying the governance schemas into HSS with a recorded source hash — sanctioned by
   the integration spec, but a one-way door through git history.
6. **Search contract alignment** — the spec's `vault.search` is POST with `kinds`/`tags` filters
   and a citation-shaped response carrying `excerpt`, `line_start`, `canonical_url`. Only the
   *path* was aligned; the shape needs snippet extraction and a public base-URL setting.
7. **Review surface** — `vault:review` is recognised and granted by no route.
   `vault_review_cases` accumulates with nothing to adjudicate them.
8. **Export/snapshot** — `vault:export`, quota already defined, for the projector.
9. **E501** (116 findings, only 6 in `app/vault/`) and **UP042** (`StrEnum`, a real behaviour
   change to Pydantic response types) both deliberately deferred.

**Blocked or out of scope until re-approved:** MCP (`mcp` is not an approved dependency, needs a
PyPI-verified version when proposed); `VAULT_ENABLED=true` in production; partial HNSW index per
profile; dimension-change DDL; a vector relevance floor.

---

## 4. Known weaknesses in what shipped

1. **`READABLE_PATH_PREFIXES` duplicates `folders.yml` by hand.** A test asserts HSS's SQL and
   Python predicates agree with *each other*; nothing detects drift from the YAML. Re-run the
   manual diff whenever either side moves — it found a real bug the first time.
2. **Rate-limit buckets are per process.** Two Gunicorn workers means the effective ceiling is
   twice the stated limit. Fine on one host; across hosts it stops being a limit.
3. **Auth costs a database round trip per request.** The price of revocation taking effect
   immediately. A cache trades exactly that away.
4. **`Reject` settles as `invalid`** — no dedicated `vault_write_request_state` value.
   Unreachable while `reject_at` is disabled; give it its own value if a policy enables it.
5. **The advisory lock serializes all governed writes.** Correct and cheap now; it is the first
   thing to reconsider if contribution throughput ever matters.
6. **`vault_documents.id` is heterogeneous** — agent slug, service-assigned uuid, or (later) an
   importer uuid. Nothing may assume a format.
7. **Human notes carry no `ID` by governance rule**, so a rename-plus-edit breaks `related_ids`
   pointing at one. Inherent to identity-less source files.

---

## 5. Environment gotchas

- The venv is in the **main repo**, not the worktree.
- **`docker ps` first** if the suite hangs. A dead daemon looks like a hang.
- Alembic reads `DATABASE_URL`; pytest reads `TEST_DATABASE_URL`. Build **both** lineages.
- **Revision ids must fit `varchar(32)`.**
- **`ruff target-version` must be the oldest runtime (3.12), never the local interpreter.** Setting
  it to 3.14 let UP037 strip quotes from forward references and broke CI at import.
- **Never pipe a check you intend to trust** — a pipeline's exit status is the last command's, so
  `cmd | tail && echo OK` always prints OK. This cost a false "verified" claim twice.
- **Patch files CRLF-mangle in transit on Windows.** Write them to their destination directly, or
  `sed -i 's/[ \t]*\r$//'`.
- Standalone scripts must set the SelectorEventLoop policy themselves on Windows.
- Quoted heredocs (`<<'PY'`) when content contains backticks; `write_bytes`/`newline="\n"` to avoid
  injecting CRLF.
- Windows/PowerShell: `curl.exe` not `curl`; `< file` unsupported; `$env:` does not persist. The
  Bash tool is Git Bash — PowerShell here-strings are a syntax error there.

The last three gotchas are also recorded as knowledge-vault notes
(`938857de…`, `d92e3fe2…`, `c618f459…`) so they outlive this file.
