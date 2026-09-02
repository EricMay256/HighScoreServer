# Handoff

Last updated 2026-09-02. The start-here for a fresh session: where things are,
what is in flight, and the environment facts that bite and are not already in
`AGENTS.md`. What remains to do is [`NEXT-STEPS.md`](NEXT-STEPS.md); why things
are the way they are is in the ADRs. Previous handoffs are archived under
[`archive/`](archive/README.md) — session logs and decision briefs kept for
their reasoning, none of them current.

## Where things are

| | |
| --- | --- |
| Branches | `dev` at `e8634b2`, 94 commits ahead of `main`; `main` is an ancestor, so the merge is a fast-forward |
| Migrations on `dev` but not `main` | vault `0018_metadata_amendments` and `0019_oauth_grant_label`; none on the leaderboard lineage |
| Production (last recorded 2026-08-28) | `VAULT_ENABLED=true`, `VAULT_PUBLIC_URL` and an operator identity set; vault lineage `0017_oauth_entitlements`; 94 documents — 80 notes and 14 wiki pages — all active |
| Local development | PostgreSQL 17 with pgvector; the vault schema lives in the `leaderboard` database; `TEST_DATABASE_URL` points at `leaderboard_test` |
| Suite | 1,172 test functions, about seven minutes; `ruff check app/ tests/ scripts/` clean |

Production facts are as the documents last recorded them. Nothing in the
2026-09-02 pass queried Heroku. Verify with `heroku config` and
`heroku run "python -m alembic -c alembic-vault.ini current"` before relying on
them.

## In flight

- **The librarian and proposal revision.** Vault ADR 0044 is Accepted and
  unimplemented; ADR 0043 is Proposed. The plan is
  [`librarian-plan.md`](librarian-plan.md).
- **The 2026-09-02 code review** —
  [`code-review-2026-09-02.md`](code-review-2026-09-02.md) — lists findings
  with fixes. The documentation fixes were applied in the same pass; the code
  fixes were not.
- **The `dev` → `main` merge** is overdue. See NEXT-STEPS §1.

## What August settled, so nobody re-litigates it

- Search is a discovery surface: no bodies, a bounded preview,
  `vault_get_note` fetches (ADR 0031).
- `flag_at` stays 1.0 on `text-embedding-3-small`; the bands overlap by
  measurement (`embedding-calibration.md`). Tags stay in the embedding text —
  removing them widened the overlap.
- Privileged tools sit on the one MCP mount behind `vault:review` (ADR 0026);
  there is no admin mount.
- Compilation plans from per-note declines, not a frontier (migration 0015),
  and `Agent/wiki/` is owned by the service since 2026-08-24.
- Edges are ids, translated to `[[wikilinks]]` only at the import and export
  boundaries (ADRs 0025, 0030).
- OAuth entitlements live on the refresh family, and a label on the family is
  display-only (ADRs 0029, 0040).
- Two consoles, two credentials: the reviewer holds `vault:read vault:review`
  and nothing else; the browser holds `vault:read vault:propose` (ADRs 0037,
  0039).

## Environment facts that bite

In addition to `AGENTS.md`, which already covers the Windows event-loop policy,
the two shells, and the worktree filesystem rule.

- **One pytest process at a time.** The autouse fixture `TRUNCATE`s, so two
  concurrent runs deadlock and produce dozens of spurious failures. A local
  PostgreSQL restart mid-run looks similar: every error reads
  `the database system is in recovery mode`, and only the modules scheduled in
  that window fail. Rerun those before suspecting the code.
- **`TEST_DATABASE_URL` in `.env.example` is a placeholder** (`role:password@…`).
  There is no `role` user locally; `.env` points at `leaderboard_test` as
  `postgres`. A sudden `password authentication failed for user "role"` means
  that line was copied back.
- **The virtualenv lives in the main checkout**, not in worktrees. Use its
  interpreter by absolute path from a worktree.
- **The vault Alembic environment ignores a worktree's `.env`** — it loads
  `Path(__file__).parents[1] / ".env"`. Pass `DATABASE_URL` explicitly.
  `app/env.py` walks up and is fine.
- **Git Bash mangles `rev:path` arguments** (`git show origin/main:file`). Set
  `MSYS_NO_PATHCONV=1`.
- **PowerShell `Set-Content -Encoding utf8` writes a BOM** on 5.1; a token read
  back from such a file carries three junk bytes into the header.
  `requirements-dev.txt` is UTF-16 today for a related reason (see the review).
- **`ruff format` is not part of this project.** Format only the lines you add.
- **Alembic revision ids must fit `varchar(32)`**; the failure lands after the
  DDL has run.
- **Never pipe a check you intend to trust** — `${PIPESTATUS[0]}`.
- **The `importer` principal name is load-bearing** for the widened quota in
  `PRINCIPAL_LIMITS`, and the write ledger is keyed
  `(principal_id, idempotency_key)`, so a differently named principal bypasses
  the duplicate guard. The runbook's corpus-migration procedure now issues a
  fresh principal per re-import on purpose; such a principal runs at the base
  quota unless it is literally named `importer`.
- **`heroku run` claims flags meant for your script** — quote the whole remote
  command. **`heroku pg:psql` is broken on this Windows install** — the
  runbook's psycopg one-liner goes around it. **`alembic current` needs
  `-c alembic-vault.ini`** or it answers the leaderboard question.
- **`issue_vault_credential` prints the secret to stdout.** An agent that runs
  it has read the token into its transcript — twice already, on live
  credentials. The person runs it.

## Durable records

- Leaderboard ADRs: `docs/adr/`. Vault ADRs: `app/vault/docs/adr/`
  (independent numbering; cite as "vault ADR 00NN").
- Runbook: `app/vault/docs/vault-configuration.md`. Architecture and deferred
  decisions: `vault-architecture.md`. Calibration register:
  `embedding-calibration.md`. Extraction checklist:
  `vault-extraction-manifest.md`.
- The MCP efficiency assessment and what came of it:
  `docs/knowledge-vault-skill-mcp-efficiency-assessment.md`.
- Validated-runs spec: `docs/specs.md`.
