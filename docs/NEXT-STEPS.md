# Next steps

Current as of 2026-08-22. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF-VAULT-IMPLEMENTATION.md` holds the context
this list assumes — inherited state, the conventions that bite, what is unsettled.
`HANDOFF.md` holds the full task list and session history; `HANDOFF-METADATA.md`
holds the metadata-model decision brief. This file exists so none of them has to be
read to know what to pick up.

**State:** on `dev`. Suite green (461 vault, 703 full). PR #14 merges the
22-commit gap into `main` and is open for review.

**Production is two revisions behind `dev`'s schema:** it sits at vault lineage
`0011_review_candidate_optional` with 70 documents, all active, none flagged,
none unembedded, every path a title slug. The review flow shipped in release
v64. `0012_document_promotion_status` and `0013_oauth_authorization_server`
deploy with the next release. Both are additive and need no backfill — 0012 adds
one nullable column and one enum, 0013 adds three empty tables — and neither
changes any running behaviour, since nothing calls the new code yet.

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

## 3. Implement ADR 0024 — the authorization server — **persistence landed**

Accepted. Migration `0013_oauth_authorization_server` and its repositories are in,
along with `app/vault/passwords.py`; the provider is still unwritten.

Done:

- ✅ `vault_oauth_clients` — registrations, `client_info` as JSONB so an RFC 7591
  field nobody modelled survives the round trip
- ✅ `vault_oauth_pending_authorizations` — the nonce store, PKCE challenge
  included, redeemed by `DELETE ... RETURNING`
- ✅ `vault_oauth_authorization_codes` — same idiom, shorter TTL. `load` does not
  consume; `redeem` does
- ✅ `app/vault/passwords.py` — bcrypt, offloaded to a thread, and
  `VAULT_OPERATOR_PASSWORD_HASH` in config rather than a table

**No token table, and there will not be one.** An access token is a
`vault_agent_credentials` row — that is the whole of ADR 0024.

The 2026-08-22 spike answered the question that blocked it — `/authorize` runs in
the operator's system browser, so Google login is viable — and left three
constraints the implementation must honour:

- **Client registrations persist to Postgres, never process memory.** Registration
  is server-to-server from the vendor's backend while authorization is a browser
  navigation, so the two halves reliably land on different workers. The spike's
  first version used a dict and failed exactly there, deterministically.
- **The `401` needs no `resource_metadata` parameter.** Clients construct the
  well-known URL by convention, so `mcp.py` does not have to change.
- **The SDK's routes break handler introspection.** slowapi reads
  `handler.__name__`; the SDK wraps some endpoints in `CORSMiddleware`, which has
  none. Label them, or the first request raises inside the rate limiter.

### The operator login page

`authorize` never authenticates inline. It returns a redirect — the SDK's own
pattern for handing off to a third party, and it works the same handing off to
ourselves:

```
GET  /authorize             SDK validates params, calls provider.authorize()
  -> redirect               to the vault's login page, carrying a nonce
GET  /vault/login?req=...   consent + password on one screen
POST /vault/login           bcrypt verify, mint the authorization code
  -> redirect               to the client's redirect_uri with code and state
```

Google login replaces the middle two steps with a redirect to Google and a
callback; everything either side is identical. Build the password path first —
it needs no external registration, so it can be exercised end to end locally.

What is left to build, roughly in order:

1. ~~**The pending-authorization store.**~~ Done — `vault_oauth_pending_authorizations`,
   redeemed atomically, with the PKCE `code_challenge` inside `params`.
2. ~~**The operator credential.**~~ Done — `app/vault/passwords.py` and
   `VAULT_OPERATOR_PASSWORD_HASH`. Config rather than a table: one secret, no
   lifecycle to model, rotation is `heroku config:set`. `bcrypt` is now listed in
   the extraction manifest as staying in both repos.
3. **The template — vault-owned.** `app/vault/templates/`, with the vault building
   its own Jinja2 environment. **Do not extend HSS's `templates/base.html`**: the
   package moves as a directory and a host asset does not move with it. This is
   the first non-doc asset in the package, so the extraction manifest needs a row
   for it, and `jinja2` joins the dependencies that stay in both repos.

   It must name the **client and the scopes requested** above the password field:
   a scope grant the operator never sees is one they did not make.
4. **CSRF on the POST.** HSS has none today and this is a public unauthenticated
   form. A signed hidden token tied to the nonce is enough; there is no session to
   hang one off.
5. **A login-specific rate limit**, tighter than the 600/min pre-auth guard. A
   public password endpoint is a brute-force target; bcrypt's cost is the first
   defence, the IP guard the second, and neither is sized for this.

Stateless by decision (ADR 0024): the password is entered per authorization, with
no session cookie. Authorizing a client is rare, and a session would be a third
credential type with its own lifetime and revocation story.

Two things to get right that are easy to miss: the redirect back to the client
must carry `state` unmodified or the client rejects it, and a failed password must
not reveal whether the nonce was valid — one message for both.

**A `grant`/`revoke-scope` subcommand on `issue_vault_credential` must land with
it.** OAuth clients all start at the read+write baseline and some will need more;
the only documented way to widen is a raw `UPDATE` on `scopes`, which does not
survive becoming routine.

`app/vault/oauth_spike.py` has served its purpose and should be deleted in the
same commit that adds the real provider — keep it until then, since its route
wiring and slowapi workaround are both reused.

## 4. Revoke the `importer` credential

`db11bca5fa415f42` (principal `importer`) still holds `vault:read vault:write
vault:update` and has no remaining purpose: the 2026-08-21 re-import ran under
`importer-b`, which is revoked. `claude-1` is the working credential.

A live write credential whose job is finished.

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
