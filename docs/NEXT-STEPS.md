# Next steps

Current as of 2026-08-22. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF-VAULT-IMPLEMENTATION.md` holds the context
this list assumes — inherited state, the conventions that bite, what is unsettled.
`HANDOFF.md` holds the full task list and session history; `HANDOFF-METADATA.md`
holds the metadata-model decision brief. This file exists so none of them has to be
read to know what to pick up.

**State:** on `dev`. Suite green (504 vault, 746 full). PR #14 merges the
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

## 5. Compilation (Phase 4)

The last markdown writer. `Agent/wiki/` is still produced by the Stage-A librarian
loop in the knowledge-platform engine, because the service has no compile path.
`vault_compile_runs` already exists with its provenance CHECK and
`ON DELETE RESTRICT`; the service and routes do not.

Three things to carry in:

- `Agent/wiki/` is an exported prefix the corpus does **not** own. It is absent
  from `CORPUS_OWNED_PATH_PREFIXES` precisely so an export cannot delete the 15
  Stage-A pages no row accounts for. Adding it to that tuple is part of this
  work, and must not happen before the service actually holds those pages.
- Wiki `SourceIDs` now come from service note ids, not filenames.
- A compiled page can be a promotion candidate (ADR 0023 admits both types), and
  `VaultPromotionService` already routes one home to `Agent/wiki/`. Nothing to
  build; it is there so a page does not land in a folder typed `Agent Note`.

No longer blocked.

## 6. The admin MCP surface

The review routes are REST-only on purpose (ADR 0019's amendment, ADR 0021's
reasoning): reading a case serves `flagged` content and deciding one publishes or
destroys a note, so those tools stay off the surface injected text can name. The
agreed destination is a **separate admin MCP server**, not the existing mount.

Unstarted, and its own design decision with its own ADR.

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
