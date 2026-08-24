# Handoff — implementing the accepted vault decisions

Written 2026-08-22, at the end of the session that shipped the exporter, the review flow, the
production corpus migration, and ADRs 0023–0025.

`docs/NEXT-STEPS.md` is the ordered list and the thing to work from. This file is the context
that list assumes: what is already true, what will bite, and what nobody has settled.

**The design work is done.** Every ADR is Accepted. Nothing below is blocked on a decision —
only on someone building it.

## Read these before touching anything

1. `docs/NEXT-STEPS.md` — the ordered work, and the only place that stays current.
2. `app/vault/AGENTS.md` — the whole file. Most invariants below are restatements.
3. The ADRs the work implements: **0023** (promotion candidacy), **0024** (OAuth), **0025**
   (the edge graph). All three were revised during review, so read the file rather than a
   summary of it.
4. In the **private knowledge-platform repository** (`knowledge-platform`, checked out wherever you keep it):
   `Vault/00 Governance/` — the AI Contribution Policy, Promotion Policy, `types.yml`,
   `folders.yml`. **These outrank every ADR.** ADR 0023 changed three of them; the current
   files are correct and the ADR describes why.

## The state you are inheriting

**Production is healthy and current.** Vault lineage `0011_review_candidate_optional`, 70
documents, all `active`, none flagged, none unembedded, every path a title slug. Two live
credentials: `importer` (which should be revoked — NEXT-STEPS item 4) and `claude-1`.

**`dev` is pushed and 21 commits ahead of `main`.** That gap is NEXT-STEPS item 1 and it is not
bookkeeping: someone went looking for the MCP setup instructions this session and could not
find them, because they were on `dev` and GitHub shows `main`.

Landed and deployed:

- The **exporter** (`app/vault/export.py`, `scripts/export_vault_markdown.py`) — byte-stable,
  idempotent, governance-validated. `Agent/` is service-authoritative and reaches markdown here.
- **`vault_path` is the title slug**, assigned under the corpus advisory lock.
- **`origin`** (migration 0010) — upstream provenance for replayed content. The August corpus
  migration re-imported all 61 notes through it, recovering two real authors, true authoring
  dates, prose `Source` values and `ClientRunID`s the first import had dropped.
- The **review flow** (migration 0011, three REST routes, `vault:review`) — accept publishes,
  reject deletes, `superseded` reserved and unreachable.
- The **knowledge-vault skill** now contributes through the service. `kv.py contribute` refuses
  in service mode and warns in Stage A.

Not built: `promotion_status`, the OAuth provider, compilation, the admin MCP.

## What is true today that shapes the work

- **`app/vault/oauth_spike.py` is a throwaway that already paid for itself.** It answered
  whether `/authorize` runs in a system browser (it does — Chrome, `sec-fetch-dest=document`,
  referred from claude.ai), and found the two constraints in NEXT-STEPS item 3. Delete it in
  the same commit that adds the real provider; its route wiring and slowapi workaround are both
  reused. It is inert unless `VAULT_OAUTH_SPIKE_ENABLED` is set, and it is currently unset.
- **`proposed_doc_type` from ADR 0022 was never built.** `promotion_status` does not collide
  with it. They are complementary if both are ever wanted: *this should be promoted* versus
  *and it should become a Concept*.
- **The governance patch for promotion is already applied** (knowledge-platform `d40bdfc`).
  `Agent/Promotion Candidates/` is canonical, engine-managed, `ai_write: engine_only`,
  `validation_mode: agent`, typed for `Agent Note` and `Wiki Page`. Do not re-derive this from
  the ADR — read the current `folders.yml`.
- **Nothing traverses edges, deliberately** (ADR 0025). `related_ids` and `source_ids` are
  stored and returned, never followed. Batch fetch is the planned surface; `neighbours` was
  considered and rejected as a quota multiplier.
- **`Human/` has never been imported.** All 70 documents are `Agent/`. Deferred decision #1
  (whole-vault read permissions) blocks it, and the wikilink-to-id bridge in ADR 0025 waits
  behind it.

## Conventions that will bite if ignored

Inherited, and still true:

- **Lint is `ruff check app/ tests/ scripts/`.** `ruff format` is *not* part of this project.
  Do not reformat unrelated files.
- **On Windows run `python run_dev.py`,** never bare uvicorn. The `sys.platform == "win32"`
  guards are deliberate no-ops on Linux.
- **`app/vault/` contains no `from app.`** and imports siblings relatively.
  `tests/vault/test_boundaries.py` enforces it.
- **Scripts need `PYTHONPATH=.`**
- Flag any Alembic revision and any new dependency rather than just adding it.

Learned this session, and each one cost a round trip:

- **An Alembic revision id must be ≤ 32 characters.** `version_num` is `varchar(32)`, and the
  failure lands *after* `upgrade()` has run, with a message that names a column you never wrote.
- **slowapi reads `handler.__name__` on every route.** The MCP SDK wraps some endpoints in
  `CORSMiddleware`, which has neither `__name__` nor `__module__`, so any request to an SDK
  route raises inside the rate limiter with a traceback naming slowapi and nothing about OAuth.
- **The vault may not use HSS's `templates/`.** The boundary test scans *imports*, so a
  `{% extends "base.html" %}` would pass every guard and only fail at extraction. The vault
  carries `app/vault/templates/` and its own Jinja2 environment.
- **`heroku run` claims flags meant for your command.** Quote the whole remote command:
  `heroku run --app X "python -m scripts.thing --id abc"`.
- **PowerShell has no inline env-var prefix.** `$env:VAR = '...'` as its own statement, use
  `.\.venv\Scripts\python.exe` explicitly, and `Remove-Item Env:VAR` afterwards.
- **`heroku logs --num` caps at 1500**, and `Select-String` is PowerShell's `grep`.
- **`issue_vault_credential` prints the token to stdout.** An agent that runs it has read the
  secret into its transcript — this happened twice to live production credentials. Have the
  human run it in their own shell.
- **Client state for OAuth must be in Postgres.** Registration is server-to-server from
  Anthropic's backend while `/authorize` is a browser navigation, so the two halves reliably
  land on different Gunicorn workers. An in-memory dict fails deterministically, and only in
  production.

## Open questions this session did not settle

- **The admin MCP surface is unstarted** and two things now wait on it: the review decision
  verb and `promotion_status`. Both are `vault:review`-gated. It needs its own ADR.
- **Deferred decision #1 (whole-vault read permissions)** still blocks any human-layer import.
  One `vault:read` scope reads everything the read policy admits.
- **Human note identity across renames.** ADR 0012 recorded that a human note's id "is stable
  only as long as the row survives — a rename-plus-edit will break references to it." ADR 0025's
  wikilink-to-id bridge inherits that, and it should be settled *before* the first edge points at
  a human note. The available fix is the one Stage A made: assign an `ID` at first import and
  write it into the file.
- **The exporter's `SeeAlso` rendering is designed and unbuilt** (ADR 0025). It is independent of
  everything else and needs no decision — an exported `RelatedIDs` is currently a uuid Obsidian
  cannot follow, so the graph is invisible in the tree a human opens the vault to browse.

---

## Session-opening prompt

Paste this to start the session this handoff was written for. It lives here so it cannot drift
from the document it refers to.

```text
I'm continuing work on the HighScoreServer knowledge vault. Start by reading
docs/HANDOFF-VAULT-IMPLEMENTATION.md, then docs/NEXT-STEPS.md — the second is the
ordered work and the first is the context it assumes.

The design phase is finished: vault ADRs 0023 (promotion candidacy), 0024 (the OAuth
authorization server) and 0025 (the edge graph) are all Accepted, and the governance
changes 0023 required are already applied in the private knowledge-platform repo.
Nothing is blocked on a decision. Read those three ADRs in full rather than a summary
— all three were revised during review, and the revisions are the interesting part.

Work from NEXT-STEPS in order unless I say otherwise. Item 1 is merging dev into main,
which matters more than it sounds: 21 commits of vault documentation are currently
invisible on the default branch.

Verify rather than assume. This codebase has repeatedly turned out to differ from what
a reasonable reading would predict, and running the thing has caught what reading it
did not — including twice this week where my own confident claim was wrong. Tell me
what you actually checked and what you could not.

Flag anything needing an Alembic revision or a new dependency instead of just doing it,
and propose an ADR for decisions that are material rather than local.
```
