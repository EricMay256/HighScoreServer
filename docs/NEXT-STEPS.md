# Next steps

Current as of 2026-08-22. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF.md` holds the full task list and session
history; `HANDOFF-METADATA.md` holds the metadata-model decision brief. This file
exists so neither has to be read to know what to pick up.

**State:** on `dev`, pushed, **13 commits ahead of `main`**. Suite green (416
vault, 642 full).

**Production is current with `dev`'s schema:** vault lineage
`0011_review_candidate_optional`, 70 documents, all active, none flagged, none
unembedded, every path a title slug. The review flow shipped in release v64.

**ADRs 0023, 0024 and 0025 are all Accepted as of 2026-08-22.** What remains is
implementation, listed below; no decision blocks it.

---

## 1. Merge `dev` into `main`

Thirteen commits, including every piece of vault documentation written this month.
This is not bookkeeping: someone looking for the MCP setup instructions could not
find them, because they were on `dev` and GitHub shows `main`. Documentation on a
non-default branch is documentation nobody reads.

Nothing on Heroku tracks `main`, so this changes no running behaviour.

## 2. Implement ADR 0023 — promotion candidacy

"Candidacy is a field, and the export projects it into a folder."

The governance side is done (knowledge-platform `d40bdfc`): the folder is now
canonical, engine-managed, and typed for `Agent Note` and `Wiki Page`. Four pieces
of code are outstanding:

- `promotion_status` enum column and its migration
- export routing on it — candidate to `Agent/Promotion Candidates/`, otherwise
  `Agent/notes/`
- the prune-guard fix: an explicit owned-prefix set, because occupancy is the
  wrong ownership signal and the last candidate would otherwise strand its file
- the `vault:review`-gated verb to set it, which lands with the admin MCP

Blocks Phase 4, which needs to know where candidates live.

## 3. Implement ADR 0024 — the authorization server

Accepted; the provider is unwritten.

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

What to build, roughly in order:

1. **The pending-authorization store.** A table holding the `AuthorizationParams`
   against a nonce, with an expiry — a few minutes is generous. Postgres, not
   memory, for the reason client registrations are: the redirect out to the form
   and back may cross workers. This is also where the PKCE `code_challenge` waits
   until `/token` redeems it.
2. **The operator credential.** One bcrypt hash, from config or its own table.
   `app/auth.py` already has `hash_password`/`verify_password` — but `app/vault/`
   may contain no `from app.`, so the vault needs its own thin wrapper and
   `bcrypt` listed in the extraction manifest.
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

Two things to carry in:

- The exporter refuses to prune a prefix the corpus does not populate. That guard
  is what stops an export deleting the 15 Stage-A wiki pages, and it stops being
  load-bearing the moment the service holds wiki documents.
- Wiki `SourceIDs` now come from service note ids, not filenames.

Blocked on ADR 0023.

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
