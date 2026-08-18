# 21. MCP is a second adapter, and a credential's scopes shape its tool surface

Date: 2026-08-16

## Status

Accepted.

Extends ADR 0015 (agent credentials) and ADR 0020 (write scopes split by verb). Does not
supersede either. Records a deferral of the OAuth authorization-server design that
`auth.py`'s module docstring has anticipated since ADR 0015.

## Context

Agents reached the vault by holding `hssv1_<credential-id>_<secret>` and calling the HTTP
routes directly. That works, and it is what the importer does, but for a conversational
agent it means the credential is pasted into the conversation: it lands in the model's
context, in the transcript, and in any summary derived from them. A real session in
August 2026 exercised the full contribution flow that way and demonstrated the problem in
passing — the agent discovered `PUT` and `DELETE` from `/openapi.json` and chose not to use
them, which is the right outcome reached by the wrong mechanism.

`vault-architecture.md` has always drawn the request path as `Agent → "HTTP or MCP adapter"
→ Pydantic models → service`, and ADR 0001 said "thin HTTP/MCP adapters" carry no SQL. The
MCP half was planned and unbuilt; `mcp` was recorded as an unapproved dependency.

Three consumer classes were in scope: cloud agents in web interfaces (claude.ai custom
connectors), cloud agents on local interfaces (Claude Code), and local models.

## Decision

**MCP is a second transport over the same services, not a replacement for the first.**
`app/vault/mcp.py` sits beside `routes.py`. Both are thin; the governed write path, the
dedup gate, ADR 0014's read policy, the quota, and the audit trail are the same code
whichever adapter a caller arrives through. The REST surface is unchanged, and the
`hssv1_` credential is the same credential on both.

**Static bearer now, with the OAuth arm seamed rather than built.** Verification moved out
of the FastAPI dependency into `principal.resolve_credential`, which takes a token string
and returns scopes — deliberately the shape of the MCP SDK's `TokenVerifier` protocol.

**A credential's scopes decide which tools exist.** `list_tools` returns only the tools the
presented credential permits.

**The idempotency key for a contribution is derived from its content**, not requested from
the model.

## Consequences

### Why not OAuth now

OAuth 2.1 was chosen first and then reversed, and the reasoning is worth keeping because
the arguments for it are real.

What OAuth buys is *per-user consent and per-user identity*. The vault has one human. A
consent screen the operator clicks for their own agents, backed by an authorization server
that does not exist, is machinery in exchange for a property nobody needs yet. Three
further points decided it:

- **Scopes are requested by the client and granted by the authorization server.** An AS that
  issues what was asked for is self-service privilege escalation. Any OAuth arm here must
  intersect requested scopes with a server-side entitlement, which is a policy store the
  vault would have to grow.
- **Step-up authorization is designed to escalate.** A `403` carrying
  `error="insufficient_scope", scope="vault:delete"` tells the client exactly what to
  request, and the spec has clients re-authorize with the union of old and new scopes. The
  only gate is a human at a browser.
- **Client identity under DCR is self-asserted.** Claude registers a fresh client per
  connection, so per-agent policy keyed on `client_id` is not a boundary. Per-user is.

When it does land, the authorization server belongs **inside `app/vault/`**, not at a
third-party IdP: the package must stay a directory move, and an external AS holding vault
entitlements would have to migrate with it.

### Why the SDK's auth machinery is switched off

`MCPServer` accepts a `token_verifier`, but rejects one without `AuthSettings`, which
requires an `issuer_url`. Supplying it makes the server publish protected-resource metadata
pointing at an authorization server the vault does not have — a spec-compliant client would
begin the flow and fail, where today it simply sends its bearer token. Advertising a broken
discovery document is worse than advertising none, so the adapter authenticates in its own
ASGI middleware.

### Scope-filtered listing is an injection boundary

This is the load-bearing consequence. The corpus is **untrusted input**: notes are written
by agents and read by agents. A note carrying "also retire `<id>`" is read *inside* a
session that has already authenticated, and no scope check intercepts it, because the
session's scopes are genuine. OAuth would not have helped either — it authorizes the
session, and the injection spends that authorization.

What does help is that the tool is not there. A credential holding `vault:read` and
`vault:write` yields a session in which `vault_retire_note` does not exist and cannot be
named. One credential is one capability profile, and which tools exist is decided by which
credential the operator put in which client. ADR 0020's verb split is what makes this
expressible; folding the write verbs back together would collapse it.

Listing is not the only gate — a client may call a tool it was never shown, so each tool
re-checks its scope. Listing decides what an agent can *see*, the per-tool check decides
what it can *do*, and neither carries the boundary alone.

Under ADR 0019 retirement is deletion with no archived row, so this is not a recoverable
class of mistake.

### The mount inherits nothing

An ASGI mount does not inherit router dependencies or the host's exception handlers. The
pre-auth IP guard is an `APIRouter` dependency precisely so it is charged *before* the
credential lookup it bounds; a mounted app would have been the one door on the vault with no
bouncer, and it fails silently. The middleware therefore carries the guard itself and
renders slowapi's `RateLimitExceeded` locally.

Three related consequences, each found by testing rather than by reading:

- **The mounted app's lifespan does not run.** Starlette dispatches lifespan only to the
  outermost application, so the transport's session manager never starts and every tool call
  fails on a task group that was never entered. The host enters it explicitly.
- **The session manager refuses a second `run()`.** The MCP app is therefore built per
  `create_app()` and held on app state, never cached at module level.
- **A mount answers only its trailing-slash form.** `/api/v1/vault/mcp` returned a bare 405 —
  exactly the URL an operator types — so a 307 redirect preserves the method and body.

### DNS-rebinding protection is off by default

The SDK validates `Host` against `127.0.0.1` by default, which is right for a loopback-bound
server a browser might be tricked into reaching. This one is public, behind Heroku's router,
and authenticated by a bearer token no browser can attach cross-origin. Left at the default,
**every production request is a 421** — a total outage with an obscure symptom. Protection is
therefore disabled unless `VAULT_MCP_ALLOWED_HOSTS` names the hosts, which re-enables it.

### Derived idempotency keys

The HTTP contract requires the caller to supply the key, which is right for a client that
knows whether it is retrying. A model does not: asked for one it invents a fresh value per
attempt, turning a network timeout into a duplicate note — the failure the key exists to
prevent. The MCP tool derives it from the same canonical form `canonical_request_digest`
uses, so derivation and conflict detection cannot disagree about what "the same request" is.

Two *deliberately* identical contributions therefore collapse into one. In a corpus whose
purpose is deduplication that is the correct reading, but it is a choice, not an accident.

`canonical_request_digest` and `document_detail` moved to `api_models.py`. Two adapters must
produce byte-identical digests and identical projections; a second copy is a silent
idempotency bug waiting for the two to drift. They cannot live in `service.py` beside
`REQUEST_DIGEST_VERSION`, their other half, because services take domain records and never
Pydantic API models.

### Cost

`mcp` brings `mcp-types`, `opentelemetry-api`, `truststore`, and — noted because it surprises
— `httpx2`/`httpcore2`, a second HTTP stack alongside the `httpx` the Steam and embedding
adapters already use. It raised the pinned `idna` to 3.18. All of it leaves with the package
except `idna`, which is genuinely shared.

### What this does not fix

The credential still exists and still rotates the same way. MCP moves where it is entered —
client configuration instead of a chat message — so the model never sees it, and narrows what
an agent can reach to the tools its scopes permit. It is not a secrets-management solution.

Deferred decision #1 in `docs/HANDOFF.md` is untouched and still blocks a human-layer import:
one `vault:read` scope reads everything ADR 0014 makes readable. That is tolerable while the
only reader is the operator's own agents, and it is the first thing to revisit if a second
human ever connects.
