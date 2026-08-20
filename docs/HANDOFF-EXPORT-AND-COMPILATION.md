# Handoff — exporter, agent metadata, promotion queue, compilation

Written 2026-08-19 at the end of the session that landed the MCP adapter and ADR 0022.
Read ADR 0022 first; this file is the execution plan for it and assumes its decisions.

## Read these before touching anything

1. `app/vault/docs/adr/0022-two-trees-one-writer-each.md` — the governing decision.
2. `app/vault/AGENTS.md` — the whole file. Several invariants below are restatements.
3. In the **private knowledge-platform repository** (`C:\Users\yarom\Code\knowledge-platform`
   on the machine this was written on):
   - `Vault/00 Governance/AI Contribution Policy.md` — where AI may read and write.
   - `Vault/00 Governance/Promotion Policy.md` — how agent memory becomes human knowledge.
   - `Vault/00 Governance/Schemas/types.yml` — 16 types, 8 statuses.
   - `Vault/00 Governance/Schemas/folders.yml` — `ai_read` / `ai_write` per folder.

**Those governance documents outrank ADR 0022 and this file.** If they disagree, they are
right and this is stale.

## The state you are inheriting

Landed and deployed (Heroku v60+, `VAULT_ENABLED=true` in production):

- MCP adapter at `/api/v1/vault/mcp/`, five tools, scope-filtered listing (ADR 0021).
- `app/vault/principal.py` — transport-neutral credential resolution, shaped like the MCP
  SDK's `TokenVerifier` so an OAuth arm can replace it without touching anything downstream.
- Pool instrumentation: `VaultPoolObserver.maximum_checked_out`, logged periodically and at
  shutdown. Production reads `peak 1/2 concurrent, 0 failures` at idle.
- `scripts/vault_load_probe.py` — concurrency probe used to close the enablement pool review.
- `PRINCIPAL_LIMITS` — `importer` has 300/min burst 60 on `contribute` and `update`.
- `app/vault/measurement.py` — the one `percentile`, shared by three scripts.

Not built: the exporter, agent-supplied metadata, the promotion queue, compilation.

## What is true today that shapes the work

- `service.py:423-426` hardcodes `doc_type="Agent Note"`, `doc_status="Active"`,
  `vault_path=f"Agent/notes/{document_id}.md"`. Contributors cannot influence any of them.
- `vault_compile_runs` **already exists** in the schema with `compiler_principal_id`,
  timing, and `error_summary`. The CHECK is `kind='note' AND compile_run_id IS NULL` OR
  `kind='wiki' AND compile_run_id IS NOT NULL`, and the provenance FK is `ON DELETE RESTRICT`.
  So compilation's persistence is built; its service and routes are not.
- `VaultScope.REVIEW` (`vault:review`) exists and is **unused**. It is the natural scope for
  the promotion queue.
- `READABLE_PATH_PREFIXES` in `app/vault/read_policy.py` already contains
  `Agent/Promotion Candidates/`, and `folders.yml` gives that folder `ai_write: allowed`.
  The queue this plan needs is already classified.

## Phase 1 — The exporter

Goal: `Agent/` notes reach markdown, so a human can browse them and compilation has files to
read. Independently useful on today's data, before any new fields exist.

- New `app/vault/export.py` plus a `scripts/` entry point. No SQL in the script; the service
  layer owns transactions, per ADR 0001.
- Projects `Agent/` **only**. Never writes `Human/` — that would breach the AI Contribution
  Policy's write rules, which are enforced by `check-policy` on `ai/` branches.
- Frontmatter must satisfy the Metadata Standard in the knowledge-platform repo. Read it;
  do not infer the shape from an existing file.
- **Idempotent and byte-stable.** Deterministic key ordering, stored timestamps, never
  `now()`. Re-running over unchanged notes must produce a zero-line diff, or the git history
  stops being an audit log.
- Do **not** filter by `READABLE_PATH_PREFIXES`. `ai_read` governs what agents are served; a
  human browsing their own vault is not that threat model.
- Decide and record: do `flagged` notes export? A librarian probably wants to see them; ADR
  0008 withholds them from agents. Whatever you choose, say why in the module docstring.
- Add `scripts/<name>.py` to `app/vault/docs/vault-extraction-manifest.md`. The manifest
  enumerates vault-owned scripts and was missed once already this session.

## Phase 2 — Retire the second writer

This is what makes Phase 1's output authoritative rather than a duplicate, and it is mostly
in the **other** repository. Do it before anything relies on either side.

- Reconciliation stops scanning `Agent/`. ADR 0012's sweep is scoped by path prefix
  specifically so this does not require rewriting it.
- The `knowledge-vault` skill (`C:\Users\yarom\.claude\skills\knowledge-vault`) currently
  drives `vault_contrib` against the markdown layer. It must reach the service instead.
- Until both are done, the Stage A engine and the service are **two writers to one tree**,
  which is the round trip ADR 0022 exists to prevent.

## Phase 3 — Agent-supplied `doc_status` and `proposed_doc_type`

- Alembic revision on the **vault lineage** (`alembic-vault.ini`, `vault_migrations/`) adding
  `proposed_doc_type TEXT` and probably `proposed_at`. Raw SQL, reviewed, no autogenerate.
- Both fields optional on `VaultContributionRequest`. `doc_status` validated against the
  Status Map for the note's *actual* type; `proposed_doc_type` validated as a known type name
  from `types.yml`.
- **`doc_type` stays untouchable by contributors, and `status` more so.** ADR 0011: `status`
  is the closed enum that gates reads, `doc_status` is free text with a shape-only CHECK. An
  agent that could set `status` could mark its own flagged note active.
- **There must be no code path from contributor input to `vault_path`.** That is the whole
  privilege argument in ADR 0022; if a reviewer cannot see that property at a glance, the
  design has drifted.
- Expose on both REST and MCP. They share `api_models`, so this should be one change, not two
  — if it is two, something has been duplicated that should not be.
- The digest rule changes shape here: adding fields to `VaultContributionRequest` interacts
  with `canonical_request_digest`'s `exclude_unset`. Re-read ADR 0016's amendment and
  migration `0006` before assuming it is free. It may need `REQUEST_DIGEST_VERSION` bumped.

## Phase 4 — Compilation and the promotion queue

- A compile service using `vault_compile_runs`: plan, write, finish, mirroring the Stage A
  librarian loop in the other repo. `kind='wiki'` rows carry `compile_run_id`.
- Notes carrying a `proposed_doc_type` are exported into `Agent/Promotion Candidates/` per
  the Promotion Policy. **Promotion into `Human/` remains a human rewriting the note** — the
  policy is explicit that a promoted note is "not a copied agent note".
- The librarian reports the pending queue **twice**: in the compile run's output, and as a
  reminder when the human is tending the human vault. This was an explicit request; a
  proposal nobody is reminded of is a proposal that rots.
- `vault:review` is the scope for any queue-reading surface.

## Conventions that will bite if ignored

- **Lint is `ruff check app/ tests/ scripts/`** via `scripts/lint.ps1` / `lint.sh`. `ruff
  format` is *not* part of this project — 16 files are unformatted and that is the status quo.
  Do not reformat unrelated files.
- **On Windows, run `python run_dev.py`,** never bare uvicorn. The `sys.platform == "win32"`
  guards in `run_dev.py`, `tests/conftest.py`, `run_mcp.py`, and several `scripts/` are
  deliberate no-ops on Linux — do not delete them as dead code from WSL. Note also that
  `uvicorn.run()` overrides a module-scope loop policy; `run_mcp.py` drives
  `Config`/`Server` under its own `asyncio.run` for that reason.
- **`app/vault/` contains no `from app.`** and imports siblings relatively.
  `tests/vault/test_boundaries.py` enforces both.
- **Scripts need `PYTHONPATH=.`** — `python scripts/x.py` puts `scripts/` on the path, not the
  repo root.
- Flag any Alembic revision and any new dependency explicitly; the manifest records what
  leaves with the package.

## Open questions this session did not settle

- **`docs/HANDOFF.md` still records the production vault gate as unresolved.** Two facts
  disagree: `VAULT_ENABLED=true` is set on `high-score-server`, and the readiness review of
  2026-08-16 classifies enablement NO-GO pending observations that could only be made once
  enabled. The load probe has now produced the pool evidence and the credential census is
  done, so this is closeable — someone needs to write the result into the review.
- **Whether `Human/` ever becomes database-authoritative.** ADR 0022 says no. If that
  changes, 0022 is superseded rather than amended, because single-writer is the whole of it.
- The `contributor` credential (`ff61acae905044a8`) is still active and its full token was
  pasted into a chat transcript on 2026-08-17.
