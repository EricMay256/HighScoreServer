# Next steps

Current as of 2026-08-28. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF-VAULT-IMPLEMENTATION.md` holds the context
this list assumes — inherited state, the conventions that bite, what is unsettled.
`HANDOFF.md` holds the full task list and session history; `HANDOFF-METADATA.md`
holds the metadata-model decision brief. This file exists so none of them has to be
read to know what to pick up.

**State: `dev` is 13 commits ahead of `main` and not yet merged.** That gap is the
MCP efficiency work — ADRs 0031, 0032, 0033 plus the edge-value validation in 0030,
the document-level embedding decision in 0034, and the summary carve-out in 0035.
Suite green (1,045 full). No Alembic revision in the range, on either lineage, so
the release phase is a no-op and the deploy is code-only.

**The one behaviour change to expect on deploy** is that `GET /api/v1/vault/search`
stops returning note bodies. A hit carries a title, a preview and ranking; the body
is a `vault_get_note` away (ADR 0031). No consumer outside this repository was
found — `scripts/vault_load_probe.py`, the docs and the tests are the callers — but
it is a breaking change to a published shape and worth naming in the PR.

**Production is current.** Vault lineage `0017_oauth_entitlements`, **94 documents**
— 80 notes and 14 wiki pages — all `active`, none flagged. Contribution has moved to
per-session OAuth credentials: four distinct `agent:oauth-*` principals have written
notes in the last three days, which is ADR 0024's per-principal attribution working
rather than a single shared `claude-1`.

**Two corpus repairs ran against production on 2026-08-28**, both idempotent and
both already verified as no-ops on a second pass:

- `scripts/resolve_vault_wikilinks.py --apply` — 21 Obsidian link *titles* that had
  been sitting in `related_ids`, a column that holds ids, across 13 wiki rows. 0
  dropped, 0 ambiguous, every original list preserved in `frontmatter`. They had
  round-tripped unnoticed for as long as the importer and exporter both passed them
  through verbatim (ADR 0030).
- A full re-export of `Agent/` into the knowledge-platform repository. The committed
  tree had been projecting an older service database, so every note's `ID`
  disagreed with the row it stands for.

**Summary coverage is the open corpus-quality gap**: 13 of 80 notes carry one,
against 14 of 14 wiki pages. It is not a display field — it joins the embedding text
and is what search returns as a hit's preview (ADR 0031) — so an unsummarized note is
measurably harder to find. Intake is fixed and the fix is holding: 14 of the last 15
notes have one, and the only gap is a single agent on 2026-08-21. What remains is
the back-catalogue.

**One deferred behaviour is now live and worth expecting.** `0015` stopped compile
planning reading the frontier; it reads per-note declines instead, and nothing is
declined. So the first plan run offers every uncovered active note rather than only
those newer than the last run's frontier. That is the intended effect — it surfaces
what the frontier had been suppressing, including anything the flagged-then-approved
bug had stranded permanently. Nothing holds `vault:compile` today, so no plan runs
until an operator issues a credential for it.

**ADRs 0023 through 0035 are Accepted and implemented.** The six numbered items
below are kept as a record of what was done and why, rather than as work to pick up.
**What is actually open is under "Open, in rough priority order" at the end**, plus
the standing items under "Deferred, unchanged" and the operator choices in item 5's
closing note.

**What 0028–0035 added, since the six items below predate them.** Amendments became
revision-bound proposals (0028) and gained a third authoring form that needs no hunk
arithmetic (0033). OAuth entitlements moved to the refresh family (0029). Edge values
are shape-checked and still never existence-checked (0030). Search became a discovery
surface (0031) and contributions report a verdict rather than the dedup gate's whole
working (0032). Embeddings were confirmed document-level in code, with chunking
deferred against a measured trigger (0034). A contributor may set a summary on its own
recent note without opening a reviewed amendment (0035).

---

# What was done, and why

The six items below are closed. They are kept because the reasoning is the part worth having later, not the tick.

## 1. Merge `dev` into `main` — **done**

[PR #14](https://github.com/EricMay256/HighScoreServer/pull/14) merged 2026-08-24,
followed by [#16](https://github.com/EricMay256/HighScoreServer/pull/16). Every piece
of vault documentation written this month is on the default branch, which was the
point: documentation on a non-default branch is documentation nobody reads.

## 2. Implement ADR 0023 — promotion candidacy — **done**

"Candidacy is a field, and the export projects it into a folder."

The governance side was already done (knowledge-platform `d40bdfc`): the folder is
canonical, engine-managed, and typed for `Agent Note` and `Wiki Page`. Three of the
four code pieces landed 2026-08-21:

- ✅ `promotion_status` enum column, migration `0012_document_promotion_status`
- ✅ routing — and it is **`vault_path`**, not a directory the exporter derives.
  ADR 0010 requires the column to equal the scanner's `rel_path`, so
  `VaultPromotionService` sets the field and moves the path in one statement under
  the corpus lock, and the exporter writes wherever the row points
- ✅ the prune-guard fix: `CORPUS_OWNED_PATH_PREFIXES` replaces the occupancy test.
  `Agent/wiki/` is written but not owned — it joins the owned set when item 5 lands
- ✅ the `vault:review`-gated verb to set it — `vault_set_promotion_status`, on the
  existing MCP mount rather than a separate admin one (ADR 0026, item 6)

No longer blocks Phase 4: candidates live where `vault_path` says, and item 5 can
read that.

## 3. Implement ADR 0024 — the authorization server — **done**

The flow works end to end: register, authorize, log in, redeem a code, use the
token against the real vault surface, refresh, and have a replayed refresh token
revoke its whole family. 21 tests drive it over HTTP.

- ✅ migrations `0013_oauth_authorization_server` and `0014_oauth_refresh_and_csrf`
- ✅ `app/vault/oauth.py` — the ten-method provider
- ✅ `app/vault/oauth_routes.py` — login page, consent screen, CSRF, route assembly
- ✅ `app/vault/templating.py` + `templates/login.html` — the vault's own Jinja2
  environment, the first non-doc asset in the package
- ✅ a login-specific rate limit (`VAULT_LOGIN_RATE_LIMIT`, default `10/minute`)
- ✅ `oauth_spike.py` deleted; its route wiring and slowapi labelling survive in
  `oauth_routes.py`
- ✅ `grant` / `revoke-scope` on `issue_vault_credential` — the only supported
  way an above-baseline scope reaches an OAuth client, replacing the raw
  `UPDATE` on `scopes` that ADR 0024 said would not survive becoming routine

**Two decisions this made that the ADR had left open**, both now amendments in it:

- **Refresh tokens, rotated with replay detection.** Without them, expiry means
  the operator redoes the browser flow every lapse, which pushes access-token
  lifetime out to weeks — the opposite of what expiry is for. With them the
  access credential lives an hour and renewal is a machine round trip. OAuth 2.1
  requires rotation *with detection* for a public client, so
  `vault_oauth_refresh_tokens` carries a `family_id` and marks `consumed_at`
  rather than deleting: a replayed token revokes the entire chain.
- **CSRF is a server-side token, not a signed one.** Signing needs a signing key
  — a third secret to configure and rotate — while a row already exists per
  authorization to hang a random token on, single-use for free.

**Enabling it in production is now a configuration step**, and nothing in the
code blocks it. Set `VAULT_OPERATOR_PASSWORD_HASH` (generate it with
`python -m scripts.hash_vault_operator_password`) and then `VAULT_PUBLIC_URL`,
which is the on/off switch: every URL in the discovery metadata is absolute, so
a deployment that cannot state its own origin publishes nothing rather than
something wrong. `app/vault/docs/vault-configuration.md` has the runbook and a
troubleshooting table.

Set the password hash **first**. With `VAULT_PUBLIC_URL` set and no hash, the
discovery documents advertise an authorization server whose login refuses every
attempt.

## 4. Revoke the `importer` credential — **done**

Revoked 2026-08-21. `db11bca5fa415f42` (principal `importer`) held `vault:read
vault:write vault:update` with no remaining purpose: the 2026-08-21 re-import ran
under `importer-b`, which was already revoked.

**Production now has exactly one active credential**, `claude-1`
(`75e92b37a057f78b`), holding `vault:read vault:write`. No live credential holds
`vault:update`, `vault:delete` or `vault:review`.

One loose end, deliberately left: `PRINCIPAL_LIMITS` still widens `contribute`
and `update` for the principal name `importer`. It grants nothing on its own —
no credential carries that name any more — and it is the right thing to keep for
the next bulk import. Note that a *new* credential named `importer` would inherit
that headroom.

## 5. Compilation (Phase 4) — **done**

The last markdown writer. `Agent/wiki/` was produced by the Stage-A librarian loop
because the service had no compile path; it has one now (ADR 0027).

- ✅ `VaultCompileService` — plan, write page, finish, fail
- ✅ four REST routes under `/api/v1/vault/compile/`, scope `vault:compile`
- ✅ `find_similar` excludes wiki pages from the dedup corpus — a latent bug that
  would have bitten the moment the first page was written, not a new rule
- ✅ `source_ids` validated and refused when unresolved, unlike `related_ids`
- ✅ `scripts/import_vault_wiki.py` — the one-off bridge, verified end to end
  against the test database
- ✅ the `_index.md` renderer, in the exporter rather than as a row
- ✅ the import ran against production (14 pages, 4 runs), and `Agent/wiki/`
  joined `CORPUS_OWNED_PATH_PREFIXES` afterwards — in that order, which was the
  load-bearing part

**It is 14 pages, not 15** — every earlier doc said 15, including this one,
and nobody counted. There is also an `_index.md` and a `_frontier.yml`. The
exporter now regenerates the index, so it is no longer at risk; `_frontier.yml`
never was, since the sweep takes `*.md` alone and its service equivalent is
`vault_compile_runs.output_frontier`.

**The order is load-bearing and the failure is silent.** `Agent/wiki/` is
exported but not owned, which is what stops `--apply --prune` deleting files no
row accounts for. Compilation existing does not change that — the gate is
whether the pages exist *as rows*, and `_index.md` will never be one. Adding the
prefix before the import, or before the exporter learns to write the index, is a
one-line diff that deletes real files. `export.py` carries the warning at the
constant.

**The import needs the migrations deployed first.** Not for the data — 0012/0013
/0014 add nothing it uses — but because `DOCUMENT_DOMAIN_COLUMNS` now names
`promotion_status` in every `RETURNING`, so the shared repository cannot run
against a database at 0011. Deploy (the release phase applies all three; they
are additive and change no running behaviour), then import.

**Verified against the test database:** all 14 pages import, the four historical
compile runs are preserved as four rows, and a re-export reproduces every file
byte-for-byte **except one line each** — `CompileRunID`, which is a uuid here
and was `run_20260813_184935` in Stage A. The grouping survives it: the nine
pages from one Stage-A run share one uuid.

The regenerated `_index.md` differs by three lines, all deliberate: `CreatedAt`
comes from the earliest page rather than the index's own first creation (which
cannot be recovered without parsing the old file), the empty `aliases:` key is
gone (the canonical renderer cannot emit one bare), and the blurb names the
generator that now writes it.

**Total one-time diff: 17 lines across 15 files.** A second export writes
nothing, which is the property that matters.

Once that lands, the knowledge-platform engine's `compile plan`/`write`/`finish`
and the `knowledge-vault` skill's compile loop can be retired: ADR 0022 gives
each tree one writer, and that becomes true of `Agent/wiki/` at that point.

## 6. The privileged MCP tools — **done**

[ADR 0026](../app/vault/docs/adr/0026-privileged-tools-are-gated-by-scope-on-one-mount.md)
settled it, and **reversed** what ADRs 0019 and 0023 both assumed: there is no
separate admin MCP. The four privileged tools live on the existing mount, gated
by `vault:review` through `list_tools`.

- ✅ `vault_list_review_cases` — ids and reasons, deliberately **no note bodies**
- ✅ `vault_read_review_case` — the only tool serving `flagged` content
- ✅ `vault_decide_review_case`
- ✅ `vault_set_promotion_status` — ADR 0023's last outstanding piece

**The operating rule, which is now the boundary:** a reviewing credential holds
`vault:read` and `vault:review` and nothing else. Then the adjudicating session
cannot also retire or overwrite. That is configuration rather than code, and it
is the accepted cost — a separate mount would have made the consumer surface
structurally incapable of destruction. Re-open the ADR if a second person gains a
credential, or if an agent starts adjudicating unattended.

`claude-1` holds `vault:read vault:write`, so a reviewer is a second credential
rather than a widening of the first:

```bash
python -m scripts.issue_vault_credential issue --name reviewer --scopes vault:read vault:review
```

---

## Deferred, unchanged

**Deferred decision #1 (whole-vault read permissions) still blocks any
human-layer import.** `folders.yml` governs `ai_write` and has no per-credential
`ai_read`; one `vault:read` scope reads everything the read policy admits.

**`superseded` is a reserved review state** with no decision path that sets it.
Leave it that way until there is a case that needs it and a reason to write down.

**The knowledge-vault skill's compile loop is retired — done, 2026-08-24.** It was
the last Stage-A writer of `Agent/wiki/`, and removing it is what made ADR 0022's
one-writer-per-tree actually true there. `python -m vault_contrib.cli compile ...`
no longer exists; compilation runs through the service's routes behind
`vault:compile`. The change lives in the knowledge-platform repository.

**Batch fetch by id is planned and deferred** (ADR 0025). `GET /notes?ids=a,b,c`
removes the round trip per hop without letting the vault walk the graph, and
without the quota multiplier a `neighbours` endpoint would introduce. Build it
when a caller needs it.

---

## Open, in rough priority order

**1. Merge `dev` into `main`.** 13 commits, 47 files, no migration. `main` is an
ancestor of `dev`, so it is a fast-forward. See the state note at the top for the one
behaviour change to call out.

**2. Backfill note summaries.** 67 of 80 production notes lack one. This is the
largest remaining retrieval-quality gap, and it is a re-embed rather than a text edit:
`summary` joins the embedding text, so changing it invalidates
`embedded_text_sha256`. `scripts/backfill_vault_summaries.py` in the knowledge-platform
engine plans the work; an agent writes the prose.

**3. Decide whether the four largest notes want splitting.** Four notes contributed on
2026-08-28 run 10.7k–15.4k characters against a corpus mean of 2.1k; the largest exceeds
every compiled wiki page. That is legal — nothing caps a body below 100k — but it is
exactly the population ADR 0034's eligibility policy was written to watch, and it
arrived the day after chunking was deferred. `scripts/measure_chunk_eligibility.py` now
has real subjects. The question is editorial before it is technical: whether each is one
insight or several.

**4. Gated on measurement, not on anyone's time.** Each of these has a written trigger
and should stay closed until it fires:

- Chunk-level retrieval — ADR 0034's evaluation gate.
- A `ts_headline` arm for search previews — ADR 0031 records why the two obvious
  shortcuts do not work, and what a real one would cost.
- Batched multi-query search — only if traces still show repeated-search overhead.

**5. The exporter and the governance linter disagree on frontmatter key order.** Every
fresh export presents as ~25 files of fixable drift, and knowledge-platform's pre-commit
hook blocks on it until `lint --fix` runs. Format-only, no value changes, but it is a
manual step on every export and it spans both repositories. Either teach the exporter
the linter's order, or have the export script run the fixer itself.
