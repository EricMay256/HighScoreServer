# Handoff — Agent corpus imported, `flag_at` calibration next

**HSS repo:** `C:\Users\yarom\Code\HighScoreServer\HighScoreServer`
**Worktree:** `.claude\worktrees\vault-readonly-slice-review-0f7ee7`
**Branch:** `ai-claude/notes-importer-script-db1469`, HEAD `4c5afaf`, tree clean.
**Knowledge platform:** `C:\Users\yarom\Code\knowledge-platform`

**`12d91bd` and `4c5afaf` are still unpushed**; `origin/dev` is `49eede7`. CI has never seen
the write path. `engine/scripts/import_to_vault_service.py` in knowledge-platform is
**untracked**.

> This file goes stale fast. The durable record is `app/vault/docs/adr/` (0001–0016) and the
> "Deferred decisions" section of `app/vault/docs/vault-architecture.md`. Treat §1 as an index
> into those and re-derive anything load-bearing from git and from the database.

---

## 0. Orientation — the local environment changed materially

**pgvector 0.8.6 is now installed in the local PostgreSQL 17.9, and the vault schema lives in
the ordinary dev database `leaderboard`.** The `hss-vault-verify` container is no longer
required for local vault work. Local HSS is now the real shared-database topology:
leaderboard tables and the `vault` schema in one database, `VAULT_DATABASE_URL` unset.

| | |
| --- | --- |
| Server | PostgreSQL 17.9, `localhost:5432`, `x86_64-windows` |
| `vector` extension | 0.8.6, built from source with MSVC, installed into `C:\Program Files\PostgreSQL\17` |
| `leaderboard` | leaderboard lineage `0004_auth_identities`; vault lineage `0004_reconciliation` |
| `search_vector` config | `'english'::regconfig` |
| Vault credential | principal `importer`, scopes `vault:read vault:write` |

The venv is in the **main repo**, not the worktree:

```powershell
& "C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.venv\Scripts\Activate.ps1"
Set-Location "C:\Users\yarom\Code\HighScoreServer\HighScoreServer\.claude\worktrees\vault-readonly-slice-review-0f7ee7"
```

Rebuilding pgvector is only necessary after a PostgreSQL **major** upgrade — the DLL does not
carry across. Instructions: an elevated shell, `Enter-VsDevShell a564ca7f -DevCmdArguments
'-arch=x64'` (or `cmd` + `vcvars64.bat`), then `nmake /F Makefile.win` and `... install` with
`PGROOT` set. Verify `cl` reports **x64** first; the x86 toolchain builds a DLL PG17 loads
silently as nothing.

---

## 1. What this session did

1. **Wrote the importer** — `knowledge-platform/engine/scripts/import_to_vault_service.py`.
   Walks `Vault/Agent/notes/`, POSTs each note to `/api/v1/vault/contributions`. Dry run by
   default; `--apply` writes. Stdlib `urllib` only, because the engine has exactly one runtime
   dependency (`pyyaml`).
2. **Installed pgvector locally** and migrated the vault lineage into `leaderboard`.
3. **Imported the corpus.** 35 notes, all `inserted`.

Verified in the database after the run:

| Check | Result |
| --- | --- |
| Documents | 35, all `status=active`, `kind=note` |
| `vault_path` | all match `Agent/notes/<uuid>.md` |
| `contributed_by` | `agent:importer` (from the credential, never the body) |
| Embeddings | 35/35 under `openai/text-embedding-3-small:1536`, all with `embedded_text_sha256` |
| Write requests | 35, all state `inserted` |
| Review cases | 0 — nothing flagged |
| Audit events | 35 × `vault.contribute` / `inserted` |
| `source_sha256` | NULL on all 35 — correct; marks them DB-authored and sweep-safe |
| Original-ID recovery | all 35 frontmatter IDs recoverable from `vault_audit_events.idempotency_key`, titles match the source files exactly |
| Lexical retrieval | `search_vector` populated on all 35; `websearch_to_tsquery` returns the expected note |

Re-running the importer is a no-op: each note's frontmatter `ID` is its idempotency key, and
the service replays its earlier response rather than writing again. This was tested against a
stub before the real run and holds in production shape.

---

## 2. Next step — calibrate `flag_at`, and amend ADR 0016

The measurement ADR 0016 deferred is now possible and **has been taken**. 595 pairs across the
35 imported documents, cosine on `text-embedding-3-small`:

| min | p50 | p90 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| 0.0417 | 0.2570 | 0.4097 | 0.5070 | 0.6277 | 0.7406 |

Closest pairs, all legitimately distinct notes that must **not** flag:

```
0.7406  Embedded Unity package imports ...  ||  Open Unity editors can keep stale Package Manager ...
0.6742  Recovering a wedged mcp-for-unity ...  ||  mcp-for-unity binds to one editor instance ...
0.6662  Unity package cache should expand ...  ||  Embedded Unity package imports ...
```

**This contradicts ADR 0016's stated premise.** The ADR argues that "unrelated prose routinely
exceeds 0.7" and that carrying Stage A's `0.85` across "would have sent a large share of the
corpus to review on its first day." Measured: exactly one pair exceeds 0.7, p99 is 0.63, and
`flag_at = 0.85` would have flagged **nothing** in this corpus.

The decision the ADR asked to defer is now a decision with data behind it. There is a wide
empty band between the corpus maximum (0.7406) and 1.0, so a threshold anywhere in roughly
0.80–0.90 flags nothing that exists today while still catching genuine near-duplicates.

Caveats that belong in the amendment, not in a silent constant change:

- 35 notes is a small sample, and a **self-selected** one — this corpus already passed Stage A
  string dedup, so it is exactly the population expected to be free of duplicates. It bounds
  the false-positive side well and says nothing about the true-positive side.
- The measurement is document-to-document. The gate compares one new contribution against the
  corpus, which is the same distribution, so the number transfers — but it will drift as the
  corpus grows and should be re-measured.
- Nothing here justifies `0.85` specifically over `0.80` or `0.90`. Pick from the gap, record
  why, and note that `1.0` was never wrong — only uncalibrated.

**Do not just edit the constant.** ADR 0016 explicitly says not to "restore" 0.85; superseding
that reasoning needs a new ADR in the vault lineage recording the measurement.

---

## 3. Remaining identified tasks

Ordered roughly by how much they block.

1. **Push `12d91bd` and `4c5afaf`.** CI has still not seen the write path.
2. **Decide the `flag_at` value and write the ADR** (§2).
3. **Revisit the note inclusion criteria to admit project-describing notes** (operator
   request, 2026-08-12). The goal: notes that describe an individual project — technology
   stack, ADR-type decisions, and other durable or evergreen facts — should be valid.
   `Vault Philosophy.md`'s stated inclusion criteria **already admit these** ("durable
   information", "help myself or my agents perform our tasks more effectively"), so the
   blocker is not the philosophy. It is two other things: the skill's authoring guidance says
   "write one self-contained insight per note", which is a gotcha-shaped mould a project
   profile does not fit; and `types.yml` gives the Agent layer only `Agent Note` and
   `Wiki Page`, while the `Project` and `Decision` types exist but their `folder_globs` point
   at the Human layer. Three plausible routes: relax the Agent Note shape, add an
   Agent-layer project type with its own folder and `folders.yml` policy entry, or route
   project profiles into the Human layer's existing types via Promotion Candidates. This is a
   governance change and wants a decision recorded, not a quiet edit.
4. **Decide whether the importer is committed** to knowledge-platform, and where. It is
   untracked at `engine/scripts/import_to_vault_service.py`. It is a one-shot migration tool
   whose job is done; the argument for keeping it is that the same script re-imports after a
   schema reset, and the argument against is dead weight in a repo that will not need it again.
5. **Measure embedding latency** — `scripts/measure_embedding_latency.py` has still never run
   against the real API, though `VAULT_EMBEDDING_API_KEY` is now live in `.env`, so the blocker
   is gone. Settles the retry budget (`_MAX_ATTEMPTS = 1`), still provisional.
6. **Human-layer sync** — the other half of ADR 0012. Needs a reconciliation entry point and a
   decision on where it runs. Staleness and deletion are designed; nothing is built.
7. **Port `resolve_context`** to close the `folders.yml` ↔ `READABLE_PATH_PREFIXES`
   duplication. The manual diff found a real fail-open divergence on its first run.
8. **Search contract alignment** — the spec's `vault.search` is POST with `kinds`/`tags`
   filters and a citation-shaped response carrying `excerpt`, `line_start`, `canonical_url`.
   Only the *path* was aligned; the shape needs snippet extraction and a public base-URL setting.
9. **Review surface** — `vault:review` is recognised and granted by no route.
10. **Export/snapshot** — `vault:export`, quota already defined, for the projector.
11. **E501** (116 findings, 6 in `app/vault/`) and **UP042** both deliberately deferred.

**Blocked or out of scope until re-approved:** MCP (`mcp` is not an approved dependency);
`VAULT_ENABLED=true` in production; partial HNSW index per profile; dimension-change DDL; a
vector relevance floor.

---

## 4. Decisions the import settled by action

- **Frontmatter loss was accepted.** The v1 contract carries title/body/tags/source_url only.
  Not transmitted, per key and note count: `ContributedBy` 35, `CreatedAt` 35, `LastUpdated` 35,
  `SchemaVersion` 35, `Status` 35, `Type` 35, **`ClientRunID` 31**, `Source` 8. Several are
  reconstructed by the service anyway (`contributed_by` from the credential, timestamps on
  insert), `Type`/`Status` are constant across this corpus, and `RelatedIDs` is empty on every
  note. The real loss is `ClientRunID` and six free-text `Source` values. Reversing this means
  extending the contract, not changing the importer.
- **`Source` is often not a URL.** Six of twelve non-empty values are prose
  (`HighScoreServer commit 4ee2709`, `UBearFramework session 2026-07-09`). `source_url` is
  `AnyUrl`, so the importer sends it only when it parses as absolute http(s) and reports the
  rest. Four notes carry a real `source_url`.
- **Identity is the service's.** Documents got fresh uuids; `vault_path` is
  `Agent/notes/<uuid>.md`, which deliberately does **not** match the slug filenames on disk.
  Mark-and-sweep reconciliation (ADR 0012) keys on `vault_path`, so it will not match these
  rows to source files — correct, because `source_sha256` is NULL and marks them DB-authored.
- **The corpus is 35 notes, not 36** as the previous handoff claimed.

---

## 5. Environment gotchas

**`.env` resolves two different ways, and one of them ignores worktrees.**
`app/env.py` uses `find_dotenv(usecwd=True)`, which walks up from a worktree and finds the main
repo's `.env` — so the app and `scripts/*` work from a worktree with no local `.env`.
`vault_migrations/env.py:22` instead hardcodes `Path(__file__).parents[1] / ".env"`, which in a
worktree points at a file that does not exist. **The vault Alembic lineage therefore needs
`DATABASE_URL` passed explicitly when run from a worktree.** Check the leaderboard lineage's
`env.py` before assuming it behaves either way.

- **`TEST_DATABASE_URL` in `.env` is still the placeholder** `role:password@localhost:5432/leaderboard_test`
  and fails authentication. Any test run must export a working URL first. `VAULT_TEXT_SEARCH_CONFIG=english`
  was added to `.env` this session; `VAULT_EMBEDDING_API_KEY` is now live there.
- The venv is in the **main repo**, not the worktree.
- Two worktrees are named `vault-readonly-slice-review-*`. This one is `-0f7ee7`; the previous
  handoff was written from `-839076`. Both sit at `4c5afaf`.
- **Revision ids must fit `varchar(32)`.**
- **`ruff target-version` must be the oldest runtime (3.12), never the local interpreter.**
- **Never pipe a check you intend to trust** — a pipeline's exit status is the last command's.
  Use `${PIPESTATUS[0]}`.
- **Patch files CRLF-mangle in transit on Windows.** Three notes in the Agent corpus are
  checked out with CRLF despite `.gitattributes`, and the engine's frontmatter regex anchors on
  `---\n`, so they fail to parse until newlines are normalized. The importer normalizes on read.
- Standalone scripts must set the SelectorEventLoop policy themselves on Windows.
- Windows/PowerShell: `curl.exe` not `curl`; `< file` unsupported; `$env:` does not persist
  across shells. The Bash tool is Git Bash — PowerShell here-strings are a syntax error there.
- **Shell state does not survive between separate command invocations.** `Enter-VsDevShell`,
  venv activation, and `$env:` assignments must share one shell with whatever uses them.

The Windows gotchas are also knowledge-vault notes (`938857de…`, `d92e3fe2…`, `c618f459…`), now
also in the vault database, so they outlive this file.
