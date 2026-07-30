# Handoff — Vault: governance alignment, credentials, and rate limits

**Repo:** `C:\Users\yarom\Code\HighScoreServer\HighScoreServer`
**Worktree:** `.claude\worktrees\vault-readonly-slice-review-839076`
**Branch:** `ai-claude/vault-readonly-slice-review-839076`, HEAD `87d8507`
**State:** committed, **12 commits unpushed**, not deployed. 379 tests pass, ruff clean.

The previous session reviewed and hardened the read-only slice. This session read the real
governance schemas, found the read surface was built against assumptions the schemas
contradict, and closed every schema-level importer blocker. **Read §1 and §2 before proposing
anything** — §1 is closed, §2 is the one thing still genuinely open.

---

## 0. Verify you are looking at the right tree

```bash
cd "C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.claude\worktrees\vault-readonly-slice-review-839076"
git log --oneline -12   # 87d8507 … d7f4a5f
git status --porcelain  # must be empty
```

Twelve commits sit on top of `5fad103`. `origin/dev` contains everything up to `5fad103`;
nothing since has been pushed, so **CI has never seen any of this**.

**The virtualenv is in the MAIN repo:**
`C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.venv\Scripts\python.exe`. There is none
in the worktree.

---

## 1. Settled this session — do not re-litigate

Vault ADRs are in `app/vault/docs/adr/`, now at **0015**. New decisions continue at 0016.

- **ADR 0009 — `doc_type TEXT`, not a second enum.** `kind` stays the coarse note/wiki
  lifecycle discriminator and keeps its role in the compile-provenance CHECK. The database
  constrains **shape only**; `types.yml` owns the vocabulary, so adding a type is a data
  change, not a migration.
- **ADR 0010 — `vault_path` is the only policy key.** There is deliberately **no**
  `policy_scope` column: a document's governance context is a *fold* over every matching
  `folders.yml` rule, and five of eight fields overlay unconditionally while
  `default_type`/`allowed_types`/`purpose` fall back to a shallower rule. No single glob names
  the result. Every glob is a literal prefix plus `/**`, which is why prefix matching and the
  `text_pattern_ops` index work.
- **ADR 0011 — `doc_status` carries the Status Map.** `status` stays the closed-enum
  visibility gate ADR 0008 depends on. A Wiki Page is `Current`/`Stub`, which the enum cannot
  represent. Never gate reads on `doc_status`.
- **ADR 0012 — mark-and-sweep over `source_sha256`.** Chosen over git-diff *despite* the vault
  being a git repo and git being the only thing that detects renames natively: a diff sees
  only committed changes and cannot self-heal. `source_sha256 IS NULL` means "no upstream
  file, authored here" — which is why the sweep **must** be prefix-scoped, or it deletes every
  agent note.
- **ADR 0013 — embedding text is title + aliases + tags + summary + body.** `Type`/`Status`
  excluded because they are columns now and filtering exactly beats matching fuzzily.
  `frontmatter` JSONB exists for faithful projection and is deliberately not embedded.
- **ADR 0014 — `ai_read` excludes at import and again at query.** Fails closed. Excluded
  folders are never imported *and* the read surface filters, because the two layers cover
  different failures.
- **ADR 0015 — agent credentials replace `VAULT_READ_API_KEY`.** `hssv1_<id>_<secret>`,
  SHA-256 stored, `401` bad/inactive, `403` missing scope.

Still settled from before and unchanged: ADRs 0001–0008.

---

## 2. The one thing still open — read permissions at whole-vault scope

**Everything else that blocked the importer is closed. This is not.**

`folders.yml` governs `ai_write` and had no read policy. The governance-side patch adds
`ai_read`, and HSS enforces it — but two things remain genuinely undecided:

1. **The patch is not applied.** `ai-read.patch` was delivered to the operator and verified
   with `git apply --check` against `knowledge-platform`, but applying it is theirs to do (that
   repo's own policy says an agent touching `Vault/00 Governance` supplies a diff). Until it
   lands, `folders.yml` and HSS's `READABLE_PATH_PREFIXES` describe the same intent through
   different artifacts.
2. **Two folder classifications were guesses**, flagged inline in the patch:
   `Human/02 Daily/` and `Human/01 Inbox/` are `forbidden`. `Human/07 People/` and
   `Human/11 Meetings/` were named explicitly by the operator; everything else follows from
   fail-closed.

**The duplication is the real cost and it is not solved.** `READABLE_PATH_PREFIXES` in HSS
mirrors `folders.yml` by hand. A test asserts HSS's SQL and Python predicates agree with each
other; **nothing can detect drift from `folders.yml`.** Closing that means porting
`resolve_context` and copying the governance schemas into HSS with a recorded source hash —
the architecture doc already plans this, and the integration spec sanctions it ("governance
implementation are not secrets"). It was not done because the runtime does not yet *need* the
schemas, and copying private-repo content into a public one is a one-way door through git
history.

---

## 3. What shipped

| Commit | What |
| ------ | ---- |
| `d7f4a5f` | `doc_type`; ADR 0009. Migration `0002`. |
| `f780a8c` | Embedding latency measurement script. |
| `3348c43` | Corrects ADR 0009 against the real schemas. |
| `7deaad2` | `vault_path` + `doc_status`; ADRs 0010, 0011. Migration `0003`. |
| `7db6ce5` | One shared embedding-timeout constant. |
| `d388043` | Ruff configuration and the fixes it found. |
| `2489f14` | `source_sha256`, `aliases`, `frontmatter`, alias search; ADRs 0012, 0013. Migration `0004`. |
| `0d36942` | `ai_read` enforcement; ADR 0014. |
| `066b412` | `/api/v1/vault/notes/{note_id}`. |
| `216136f` | Embedding text assembly + hash. |
| `93eea1d` | Agent credentials; ADR 0015. |
| `87d8507` | Per-principal rate limiting. |

**New modules:** `auth.py`, `read_policy.py`, `rate_limit.py`, `embedding_text.py`,
`scripts/issue_vault_credential.py`, `scripts/measure_embedding_latency.py`.

### Four things that were nearly wrong

1. **`array_to_string` is STABLE**, so PostgreSQL rejects it in a generated column outright —
   this would have failed at migration time. The tempting fix, `array_to_tsvector`, is
   IMMUTABLE but emits *unstemmed* lexemes (`'Postgres'`) that never match a stemmed query
   (`'postgr'`) — it would have compiled and silently returned nothing. Hence
   `vault.text_array_to_string`, and an alias test whose search term appears in **no other
   field**.
2. **The credential token splits from the right.** `vault_agent_credentials_id_format` permits
   `_` in an ID; a left split truncates such IDs and fails to authenticate valid credentials.
   Secrets are hex so the last `_` is unambiguous. A test pins an underscore-bearing ID because
   the naive implementation passes every other case.
3. **The first rate-limit eviction rule was a real bug**, caught by its own test: dropping a
   bucket because it *looked* full after refilling would hand the next request a fresh full
   bucket and forget everything charged, silently multiplying the limit. Pruning now requires
   elapsed time to *prove* a refill.
4. **ADR 0009 shipped with two factual errors** (ten types where `types.yml` has sixteen;
   "Summary Notes" for the singular "Summary Note") and an oversold argument — for the Agent
   layer, `allowed_types` makes `doc_type` near-isomorphic to `kind`. Corrected in `3348c43`;
   the decision survives on a better reason (the projector needs `Type` to round-trip).

---

## 4. Verified, and how

- **379 tests pass** (316 inherited + 63 added), ruff clean under the new configuration.
- **Regression discipline:** every constraint was proved by dropping it and confirming the
  tests fail. Note that a dropped CHECK lets malformed rows *commit*, which then blocks
  re-adding it — clean up by id prefix before restoring.
- **The governance patch was verified by running the operator's real engine** with it applied:
  People/Meetings resolve `forbidden`, `Human/01 Inbox/AI/` resolves `allowed` under a
  `forbidden` parent, and an unclassified folder resolves `forbidden` from the catch-all alone.
- **`array_to_string` volatility and `array_to_tsvector` lexeme behaviour** were confirmed
  directly against PostgreSQL 17, not assumed.
- **`issue_vault_credential.py`** was exercised end to end (issue → list → revoke → re-revoke
  exit 1) against the container.
- **Migrations round-trip** base→head→base→head via `test_migrations`.

**Not verified:** push and CI (never pushed), Heroku deployment, any live OpenAI call this
session, behaviour at realistic corpus size, and the governance patch *applied* (the operator
holds it).

---

## 5. Known weaknesses in what shipped

1. **`READABLE_PATH_PREFIXES` duplicates `folders.yml` by hand.** See §2.
2. **Rate-limit buckets are per process.** Two Gunicorn workers means the real ceiling is
   twice the stated limit. Fine on one host; across hosts it stops being a limit. Documented,
   not hidden.
3. **Authentication now costs a database round trip per request.** That is the price of
   revocation taking effect immediately. A cache trades exactly that away and should be a
   deliberate decision.
4. **The search response shape does not match the spec.** The spec's `vault.search` returns
   citations with `excerpt`, `line_start`, `section`, `canonical_url`, and takes `kinds`/`tags`
   filters over POST. HSS returns its own flatter shape over GET. Only the *path* was aligned —
   renaming the method without the shape would look compliant while returning something else.
5. **`EXCLUDED_PATH_PREFIXES` is empty.** Deliberate: a union cannot express a `forbidden`
   folder nested under an `allowed` one, and discovering that when someone adds such a rule is
   too late. A test asserts every listed exclusion sits inside a readable prefix.
6. **`vault_documents.id` is now heterogeneous** — an agent slug or a generated UUID for
   imported human notes. Nothing may assume a format. Human notes carry no `ID` by governance
   rule (verified: 0 of 80), so a rename-plus-edit will break `related_ids` referring to one.
7. **Ruff still excludes E501** (116 findings, only 6 in `app/vault/`) and **UP042**, which
   wants seven `(str, Enum)` classes converted to `StrEnum` — a genuine behaviour change to
   Pydantic response types, worth doing deliberately with tests.

---

## 6. Outstanding work, in the order I would take it

1. **Apply the governance patch** (`ai-read.patch`) in `knowledge-platform`, adjusting the two
   guessed folders. Everything HSS-side already assumes it.
2. **Push and let CI run.** Twelve commits, four migrations, a new auth mechanism — none of it
   has been through CI.
3. **Measure embedding latency** — `VAULT_EMBEDDING_API_KEY` is commented out in `.env`, so
   this is one command away once a key is available. Settles the retry budget
   (`_MAX_ATTEMPTS = 1`), still provisional.
4. **The importer.** Schema-wise unblocked. **It probably does not belong in HSS** — it must
   read Markdown from the private repo, which HSS has no checkout of. Decide whether it is a
   knowledge-platform script calling the HSS write API, or an operator job with direct database
   access. That choice is unmade and gates everything after it.
5. **Port `resolve_context`** to close §2's duplication, if and when the schema copy is agreed.
6. **The write path** — `vault:write` is recognised and granted by no route. ADR 0004 keeps
   `vault_contrib.core.decide()` normative, to be ported verbatim with its tests.
7. **Search contract alignment** (§5.4), which needs excerpt extraction and a public base-URL
   setting.

**Still blocked, unchanged:** MCP (`mcp` is not an approved dependency and needs a
PyPI-verified version when proposed); `VAULT_ENABLED` in production; any `Procfile` change;
partial HNSW index per profile; dimension-change DDL; a vector relevance floor.

---

## 7. Environment gotchas

- **The venv is in the main repo, not the worktree.**
- **Local PostgreSQL has no pgvector.** Use the pinned container:
  ```bash
  docker run -d --name hss-vault-verify -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=highscore_test -p 55432:5432 pgvector/pgvector@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87
  ```
  If it exists but is stopped, `docker start hss-vault-verify`. **A dead Docker daemon presents
  as a hang, not an error** — if the suite hangs, `docker ps` first.
- **Alembic reads `DATABASE_URL`; pytest reads `TEST_DATABASE_URL`.** Build both lineages.
- **Revision IDs must fit `varchar(32)`** — `0003_document_vault_path_and_doc_status` was 38
  characters and failed *after* the DDL ran.
- **Standalone scripts must set the SelectorEventLoop policy themselves** on Windows;
  psycopg3's async pool cannot use ProactorEventLoop. Caught by running
  `issue_vault_credential.py`, not by review.
- **Quoted heredocs (`<<'PY'`) when the script contains backticks** — an unquoted one turned
  `` `forbidden` `` into a command substitution.
- **Scripted rewrites can inject CRLF.** Use `newline="\n"`; check with
  `open(f,'rb').read().count(b'\r\n')`.
- Windows/PowerShell: `curl.exe` not `curl`; `< file` unsupported; `$env:` does not persist.
  The Bash tool is Git Bash — PowerShell here-strings are a syntax error there.

## 8. Verification recipe

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:55432/highscore_test" VAULT_TEXT_SEARCH_CONFIG=english
python -m alembic upgrade head
python -m alembic -c alembic-vault.ini upgrade head
```

```bash
export TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:55432/highscore_test" VAULT_ENABLED=true HSS_DB_POOL_MAX_SIZE=5 HSS_PROCESS_COUNT=2 DATABASE_CONNECTION_LIMIT=20 DB_OPERATIONAL_CONNECTION_RESERVE=2 VAULT_DB_POOL_SIZE=1 VAULT_DB_POOL_TIMEOUT_SECONDS=5 VAULT_TEXT_SEARCH_CONFIG=english API_KEY=ci-test-api-key JWT_SECRET=ci-test-jwt-secret
python -m pytest -q
python -m ruff check app/ tests/ scripts/ vault_migrations/
```

**Baseline is 379 passing.** Stop the container when finished:
`docker stop hss-vault-verify`.
