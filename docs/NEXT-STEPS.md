# Next steps

Current as of 2026-08-22. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF-VAULT-IMPLEMENTATION.md` holds the context
this list assumes — inherited state, the conventions that bite, what is unsettled.
`HANDOFF.md` holds the full task list and session history; `HANDOFF-METADATA.md`
holds the metadata-model decision brief. This file exists so none of them has to be
read to know what to pick up.

**State:** on `dev`. Suite green (538 vault, 780 full). PR #14 merges the
22-commit gap into `main` and is open for review.

**Production is three revisions behind `dev`'s schema:** it sits at vault lineage
`0011_review_candidate_optional` with 70 documents, all active, none flagged,
none unembedded, every path a title slug. The review flow shipped in release
v64. `0012_document_promotion_status`, `0013_oauth_authorization_server` and
`0014_oauth_refresh_and_csrf` deploy with the next release. All three are
additive and need no backfill — one nullable column, one enum, four empty
tables — and none changes any running behaviour: the OAuth routes are not even
registered unless `VAULT_PUBLIC_URL` is set, which it is not.

**ADRs 0023, 0024 and 0025 are all Accepted as of 2026-08-22.** What remains is
implementation, listed below; no decision blocks it.

---

## 1. Merge `dev` into `main` — **PR open**

Twenty-two commits, including every piece of vault documentation written this month.
This is not bookkeeping: someone looking for the MCP setup instructions could not
find them, because they were on `dev` and GitHub shows `main`. Documentation on a
non-default branch is documentation nobody reads.

Nothing on Heroku tracks `main`, so this changes no running behaviour.

[PR #14](https://github.com/EricMay256/HighScoreServer/pull/14) is open and
fast-forwardable. Awaiting a human on the merge button.

## 2. Implement ADR 0023 — promotion candidacy — **done except the verb**

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
- ⬜ the `vault:review`-gated verb to set it, which lands with the admin MCP
  (item 6). The service method exists and is tested; it has no transport surface

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

## 5. Compilation (Phase 4) — **built; one operator step left**

The last markdown writer. `Agent/wiki/` was produced by the Stage-A librarian loop
because the service had no compile path; it has one now (ADR 0027).

- ✅ `VaultCompileService` — plan, write page, finish, fail
- ✅ four REST routes under `/api/v1/vault/compile/`, scope `vault:compile`
- ✅ `find_similar` excludes wiki pages from the dedup corpus — a latent bug that
  would have bitten the moment the first page was written, not a new rule
- ✅ `source_ids` validated and refused when unresolved, unlike `related_ids`
- ✅ `scripts/import_vault_wiki.py` — the one-off bridge, verified end to end
  against the test database
- ⬜ **run the import against production, add the `_index.md` renderer, then add
  `Agent/wiki/` to `CORPUS_OWNED_PATH_PREFIXES`.** In that order.

**It is 14 pages, not 15** — every earlier doc said 15, including this one,
and nobody counted. There is also an `_index.md` (a generated `MoC`) and a
`_frontier.yml`; only the first is at risk, since the sweep takes `*.md` alone.

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
and was `run_20260813_184935` in Stage A. A one-time 14-line diff, and the
grouping survives it: the nine pages from one Stage-A run share one uuid. A
second export writes nothing.

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

**The knowledge-vault skill's compile loop is its last Stage-A writer.** Note
contribution moved to the service; wiki compilation did not, and cannot until
item 5 lands.

**Batch fetch by id is planned and deferred** (ADR 0025). `GET /notes?ids=a,b,c`
removes the round trip per hop without letting the vault walk the graph, and
without the quota multiplier a `neighbours` endpoint would introduce. Build it
when a caller needs it.
