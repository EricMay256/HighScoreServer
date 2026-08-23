# 24. The vault runs its own authorization server, and an OAuth token is a credential row

Date: 2026-08-21

## Status

Accepted 2026-08-22.

Design settled. The 2026-08-22 spike confirmed `/authorize` runs in the operator's system
browser, so both identity methods are reachable, and left three constraints recorded under
"Consequences".

**Implemented 2026-08-21.** Nothing in this ADR is outstanding; enabling it in production is
a configuration step (`VAULT_PUBLIC_URL` and `VAULT_OPERATOR_PASSWORD_HASH`), documented in
`docs/vault-configuration.md`.

- Migration `0013_oauth_authorization_server` — `vault_oauth_clients`,
  `vault_oauth_pending_authorizations`, `vault_oauth_authorization_codes`, and
  `app/vault/passwords.py`. There is deliberately no table for access tokens, which is this
  ADR's whole point.
- Migration `0014_oauth_refresh_and_csrf` — `vault_oauth_refresh_tokens` and a CSRF token on
  the pending authorization. See the amendment below; refresh tokens are a decision this ADR
  did not make and now does.
- `app/vault/oauth.py` — the ten-method provider. `app/vault/oauth_routes.py` — the login page
  and route assembly. `app/vault/templating.py` and `app/vault/templates/login.html` — the
  vault's own Jinja2 environment, the first non-documentation asset in the package.
- `oauth_spike.py` is deleted, its two reusable parts — the route wiring and the slowapi
  labelling workaround — carried into `oauth_routes.py`.
- `scripts/issue_vault_credential.py` gains `grant` and `revoke-scope`, which this ADR requires:
  they are the only supported way an above-baseline scope reaches an OAuth client, and they
  replace the hand-written `UPDATE` on `scopes` that did not scale to a routine operation.

Four implementation choices are recorded under "Consequences": where the operator hash lives,
why the code table's scope CHECK is wider than the baseline, why absence of `VAULT_PUBLIC_URL`
is the on/off switch, and why the login POST redeems the nonce before checking the password.

## Amendment, 2026-08-21: refresh tokens, rotated with replay detection

This ADR said OAuth credentials "should carry `expires_at` rather than living forever" and did
not say how a client renews one. The two answers are materially different, so it is recorded
here rather than settled in code.

**Decision: the vault issues refresh tokens. They rotate on every use, and a replayed one
revokes its whole family.**

Without them, expiry means the operator redoes the browser flow — password and all — every
time an access token lapses, and the connector is broken until they notice. That pushes the
access token's lifetime out to weeks to stay tolerable, which is the wrong direction: the
window a stolen token is useful for is exactly what expiry exists to shrink. With refresh, the
access credential lives an hour and renewal is a machine round trip, so the operator authorizes
once a month rather than once a lapse.

The cost is a fourth table and one property that has to be got right. **Rotation without replay
detection is not enough** — OAuth 2.1 requires a public client's refresh token to be
sender-constrained or rotated with detection, and sender-constraining is not available here. So
`vault_oauth_refresh_tokens` carries a `family_id` constant across a chain and marks
`consumed_at` rather than deleting the row. That is the one place this schema departs from the
`DELETE ... RETURNING` idiom the other two transient tables use, and the departure *is* the
security property: a deleted row cannot be told from a token that never existed, while a
consumed one is positive evidence that a token was captured. Presenting one revokes every
credential ever minted in the chain. The honest client re-authorizes; whoever stole the token
gets nothing.

Rotation also explains a consequence worth naming: **credential rows accumulate**, one per
refresh, all but the newest revoked. That is a pruning story, not a leak — they are revoked
rows, so they grant nothing.

`scripts/prune_vault_oauth.py` handles it, **by age and not by keeping the last N per
identity**. The count-based rule is the one that suggests itself and it has a failure mode
that looks like success: an OAuth principal is `oauth-<slug(client_name)>`, so two separate
registrations both naming themselves "Claude" share a principal, and keep-newest-N would
delete the older registration's *live* credential. Age plus `revoked_at IS NOT NULL` cannot
express that mistake. Thirty days matches the refresh token's own lifetime, and operator-issued
credentials are excluded by the principal prefix — those rows are a census someone reads, not
machine turnover.

This does not weaken the token-is-a-credential property. A refresh token is not an access
token: it names one, mints its replacement, and never authenticates a request.

Supersedes the OAuth deferral in ADR 0021, which recorded that the SDK's `token_verifier` was
left unused because it requires `AuthSettings.issuer_url`, and setting that would publish
discovery metadata for an authorization server that did not exist. This ADR builds the server.

Depends on ADR 0015 (operator-issued agent credentials), ADR 0020 (scopes split by verb), and
ADR 0021 (the MCP adapter and its scope-shaped tool surface).

## Context

The vault authenticates with `hssv1_<credential-id>_<secret>` bearer tokens, minted by an
operator with database access. Every client that can send a header works: Claude Code, the
desktop app, `curl`, CI.

The claude.ai web client cannot. Its custom-connector dialog takes a name, a URL, and optional
OAuth client credentials — there is **no field for a static header**. Left blank, it falls back
to the MCP specification's OAuth flow, which the vault answers with a bare
`WWW-Authenticate: Bearer` and no discovery documents:

```
POST /api/v1/vault/mcp/                    →  401, WWW-Authenticate: Bearer
/.well-known/oauth-authorization-server    →  404
/.well-known/oauth-protected-resource      →  404
```

So the client is told to authenticate and given nowhere to do it. This is not a bug; it is
ADR 0021's deferral, observed from the outside.

Three facts make building the server much cheaper than it first appears, and all three were
checked rather than assumed:

- **The MCP SDK already ships an authorization server.** `mcp/server/auth/` provides
  `create_auth_routes()` for `/authorize`, `/token`, `/register`, `/revoke`, plus RFC 8414
  authorization-server metadata and RFC 9728 protected-resource metadata. What is missing is an
  implementation of the ten-method `OAuthAuthorizationServerProvider` protocol.
- **`principal.resolve_credential` already has the `TokenVerifier` shape**, deliberately, since
  ADR 0015. `ProviderTokenVerifier` bridges the provider to it. The seam was left for this.
- **`bcrypt` is already a dependency** (`requirements.txt`, used by `app/auth.py`), with the
  cost-factor and GIL reasoning already worked out. Human password verification needs no new
  package.

## Decision

**The vault hosts its own OAuth 2.1 authorization server, mounted in this application beside
the MCP adapter, and an issued access token is backed by a row in
`vault_agent_credentials`.**

### An OAuth token is a credential, not a parallel identity

Authorization mints a credential row and the access token maps to it. This is the load-bearing
choice, and everything else follows from it:

- `contributed_by` derives from the principal, so a note contributed from the web carries real
  provenance instead of a second, differently-shaped identity.
- Revocation already exists. `issue_vault_credential revoke` and the credential census work
  unchanged, and an OAuth client appears in `list` beside every other credential.
- Scopes already live on the credential row, so ADR 0020's per-verb grants and ADR 0021's
  scope-shaped tool surface keep working through the new path without a second mechanism.
- `PRINCIPAL_LIMITS` and the quota buckets key on the principal, which an OAuth-minted
  credential has like any other.

The alternative — OAuth tokens as their own principal type — would duplicate scopes,
revocation, quotas, and attribution, and every one of those duplicates is somewhere the two
paths could disagree about what a caller may do.

### Registration is open; authorization is the gate

Dynamic client registration is enabled, because the web client has no client id to present and
the specification expects to self-register. That means **anyone can register a client**, which
is by design and is why it grants nothing on its own: a registered client still has to complete
an authorization the operator personally approves.

`ClientRegistrationOptions` sets read and write as the **baseline**, not a ceiling:

- `default_scopes`: `vault:read`, `vault:write` — what a self-registering client receives
- `valid_scopes`: `vault:read`, `vault:write` — the most a client may *request*

The distinction matters. A client can never ask for more than the baseline, so
`vault:update`, `vault:delete`, and `vault:review` are unreachable by request — a
self-registering client asking for them is refused by construction rather than by an operator
noticing on a consent screen. But the credential row is an ordinary row, and an operator may
widen a *specific* one afterwards.

That asymmetry is the point: above-baseline scopes are **granted deliberately, never
requested**. It is the same rule migration `0007` established when it split the write verbs and
deliberately granted nothing — "a migration reruns, and one that re-applies privilege would
silently restore permissions on every rebuild, rollback, or staging refresh."

Widening is therefore expected rather than exceptional, which it was not before: every OAuth
client starts at the baseline, and some will need more. `issue_vault_credential` has no
subcommand for it — the documented method is a raw `UPDATE` on `scopes` — and that does not
scale to a routine operation against production. **This ADR requires a supported
grant/revoke-scope command** before OAuth ships. The `scopes` column already carries a CHECK
constraining it to the known set, so the command is thin; what it buys is that changing a
privilege stops being hand-written SQL.

### Two identity methods, both built, chosen by configuration

`/authorize` must authenticate a human, and the vault has never had human authentication. It
gets two, because neither is reliably available everywhere the vault needs to be reachable:

- **Google (OIDC).** `authorize` redirects to Google; the callback verifies the returned
  `id_token` and treats a verified `email` on a configured allowlist as the operator. Free at
  any scale this reaches.
- **Operator password.** A single bcrypt-hashed secret, verified against a form the vault
  serves itself.

Both end the same way — a redirect back to the client carrying an authorization code — so the
method is a property of the deployment rather than of the protocol, and the rest of the
provider is indifferent to which ran.

Neither is "the fallback", and that is deliberate. **Google refuses OAuth inside embedded
webviews**, returning `disallowed_useragent`. It is enforced on Google's side, has been since
2021 and tightened in 2023, and there is no setting that disables it. So if a client performs
connector authorization in an in-app webview rather than a system browser, Google login is
impossible there and the password form — being the vault's own page — is the only method that
works. If it uses a system browser, Google is the lower-friction one, materially so on a phone,
where typing a long password is exactly the friction being removed.

Which method is preferable is therefore an empirical property of the client, not something to
settle by argument. Build both; let the deployment choose. **Verify the target client before
building the provider**: if the intended one blocks Google, that is worth knowing while it is
still a configuration question rather than a rewrite.

Neither method needs a new dependency, which was checked rather than assumed: `python-jose` and
`cryptography` verify the `id_token` against Google's JWKS, `httpx` makes the outbound call, and
`bcrypt` is already used by `app/auth.py`. `app/steam_auth.py` is the house pattern for
verifying a third-party identity over HTTP — async `httpx`, explicit timeout, typed handling of
`HTTPStatusError` separately from transport failure — and the Google callback should look like
it.

Plain SHA-256 is correct for `hssv1_` secrets because they are machine-generated with full
entropy (ADR 0015). It is **not** correct for a human-chosen password, and that distinction is
the whole reason bcrypt appears here.

### What the password step looks like

A page this server renders. `authorize` does not authenticate inline — it returns a redirect,
which is the SDK's own pattern for handing off to a third party, and works the same handing
off to ourselves:

```
GET /authorize            client arrives; the SDK validates and calls provider.authorize()
  -> redirect             to the vault's own login page, carrying a nonce
GET  /vault/login?req=..  an HTML form: what is being authorized, and a password field
POST /vault/login         bcrypt verify, mint the authorization code
  -> redirect             to the client's redirect_uri with code and state
```

**The template is the vault's own, not the host's.** HSS renders Jinja2 from a root-level
`templates/` directory, and reaching for its `base.html` would be the exact comingling the
extraction manifest exists to prevent: `app/vault/` moves as a directory, and a page depending
on a host asset does not move with it. So the vault carries `app/vault/templates/` and builds
its own environment, the same way it builds its own `Limiter` rather than sharing HSS's.

That makes this the first non-documentation asset inside the package, and the manifest has to
say so — the extraction is a directory move only for as long as everything the package needs
is inside it. `jinja2` joins `httpx`, `SQLAlchemy` and `slowapi` as a dependency that stays in
both rather than leaving.

Rendering one form without a template engine at all is a legitimate alternative — it is one
page — and the reason not to is that a login page is exactly where HTML-escaping mistakes turn
into injected markup on a form that takes a password.

**Login and consent are the same page.** It names the client and the scopes it asked for above
the password field, because a scope grant the operator never sees is a scope grant the operator
did not make — and ADR 0021's whole argument is that what a credential holds decides what tool
surface exists.

Three things it needs that do not exist yet:

- **A pending-authorization store.** The request parameters have to survive the redirect out to
  the form and back. Postgres, for the reason client registrations are: the two halves may land
  on different workers.
- **CSRF protection on the form POST.** HSS has none today, and this is a public unauthenticated
  form.
- **Its own rate limit, tighter than the pre-auth guard.** A public password endpoint is a
  brute-force target. Bcrypt's cost factor is the first defence and the IP guard the second, but
  a login-specific bucket is the one sized for this.

**Stateless: the password is entered per authorization, with no session cookie.** Authorizing a
client is rare, and a session would be a third credential type with its own lifetime, storage,
and revocation story — none of which this needs. If it ever becomes tedious, that is the moment
to reconsider, not before.

**One failure message, whatever failed.** A wrong password, an expired nonce, and a nonce that
never existed all render identically. This is ADR 0015's rule about `401` and `403` applied to
a form: the vault already refuses to say *which* check failed, and a login page that
distinguishes "bad password" from "unknown request" hands an attacker a probe for valid
authorization attempts. The operator loses nothing — they know which of the two they just
did.

### It mounts on this server

Not a separate deployment. The authorization server, the resource server, and the MCP adapter
are one application, which keeps issuer and resource on one origin and avoids a second thing to
operate.

The authorization endpoints are necessarily **unauthenticated and public** — that is what an
authorization server is — so they are the first such surface the vault has had. The pre-auth IP
guard (`rate_limit.py`) must cover them, for the reason it covers the MCP mount: without it they
are the only unbounded door, and nothing fails until someone hammers them.

## Consequences

### The 401 has to start pointing somewhere

Today's bare `WWW-Authenticate: Bearer` becomes one carrying `resource_metadata`, and
`/.well-known/oauth-protected-resource/api/v1/vault/mcp` starts answering. That is what turns
the current dead end into a flow, and it is also the moment the vault begins advertising an
authorization server — so it must not ship before the server actually works.

### Registered clients accumulate, and need an expiry

Open registration means unbounded rows. `ClientRegistrationOptions.client_secret_expiry_seconds`
exists for this, and the credential rows OAuth mints should carry `expires_at` rather than
living forever like an operator-issued one. A pruning story is required, not optional — this is
the same shape as `prune_idempotency_keys.py`.

### The mobile premise held (measured 2026-08-22)

Reaching the vault from a phone is the reason web access matters at all, and it depended on
whether the client authorizes connectors in a system browser or an embedded webview. Google
works in the first and is refused in the second.

`oauth_spike.py` answered it against the real client. The flow splits across two callers:

```
POST /register   ua='python-httpx/0.28.1'                       <- Anthropic's backend
GET  /authorize  ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64)
                     ... Chrome/151.0.0.0 Safari/537.36'        <- the operator's browser
                 sec-fetch-dest='document'  referer='https://claude.ai/'
```

**`/authorize` is a genuine top-level navigation in the system browser**, so Google is
reachable and the webview block does not apply. Registration is server-side, which is a
separate and useful fact: the two halves arrive from different addresses and therefore
different workers, so a provider must not keep client state in process memory. The spike's
first version did, and failed exactly there.

The mobile case follows from this rather than needing its own test: the claude.ai mobile app
exposes no connector settings and consumes the configuration established on desktop or web, so
authorization happens in the browser above and a phone never renders Google's login page. The
residual risk is re-authorization initiated from mobile, which the password method covers.

Both identity methods stay in the decision regardless. What this measurement settles is which
one to reach for first, and it is Google.

### Two things the persistence layer had to settle (2026-08-21)

**The operator hash is configuration, not a table.** The decision above says "from config or
its own table" without choosing. It is `VAULT_OPERATOR_PASSWORD_HASH`, because there is exactly
one secret, it has no lifecycle a schema would model, rotation is `heroku config:set` — which
is also the revocation story — and a database's backups circulate more widely than a config
var's do. Unset is a supported state meaning the password method is not configured for this
deployment, and it must never be read as "any password works": the login refuses outright, the
way `VAULT_ENABLED` defaulting to false serves no vault rather than an unguarded one.

**The authorization-code table's scope CHECK mirrors `vault_agent_credentials`, not the OAuth
baseline.** The baseline is what a *client may request*, and this ADR is explicit that an
operator may widen a specific credential afterwards — "expected rather than exceptional". A
CHECK constraining the column to `vault:read` and `vault:write` would forbid the widened case
at a layer no application code could permit, turning a supported operation into an integrity
error. The narrower rule belongs where it is enforceable and overridable: application code.

**Absence of `VAULT_PUBLIC_URL` is the on/off switch, and there is no second flag.** The spike
had one (`VAULT_OAUTH_SPIKE_ENABLED`) because it needed to be inert while it existed. The real
server needs the opposite: a deployment that cannot state its own origin *cannot* publish
correct discovery metadata, because every URL in it is absolute. So the variable that makes the
feature work is the variable that turns it on, and forgetting it fails closed — serving no
metadata, which this ADR already argues is better than advertising an authorization server
before one answers. A separate boolean would be a way to set one and not the other.

**The login POST redeems the nonce before it checks the password.** Deliberately, and the order
is the security property: one authorization affords exactly one password attempt, so a live
request cannot be reused as an unlimited guessing oracle even inside the rate limit. It costs
the honest operator a restart from the client after a typo, which is the right trade for a
public unauthenticated form.

**CSRF is a server-side token, not a signed one.** `docs/NEXT-STEPS.md` suggested "a signed
hidden token tied to the nonce". Signing needs a signing key — a third secret to configure,
rotate, and get wrong — while a row already exists per authorization to hang a random token on,
which makes it single-use for free and needs no key at all. Only its digest is stored, for the
reason every other secret in this schema is hashed.

### Existing credentials are unaffected

`hssv1_` tokens keep working exactly as they do. This adds a way to obtain a credential; it does
not change what a credential is or how one is verified. A deployment that never registers an
OAuth client is unchanged in every observable way.

### A web session is a different trust context

The corpus is untrusted, agent-written text. ADR 0021's defence against injected instructions is
that a destructive tool is absent from the surface the text can name, and restricting OAuth to
read and write preserves that — a web-authorized client has no retire tool to be talked into
using. This is why the scope restriction above is a security decision rather than a
convenience one.

### What this does not decide

Whether the operator password becomes delegated login, and whether OAuth clients ever obtain
`vault:review` — ADR 0026 has since decided there is no separate admin surface, so the question is now purely whether an operator ever grants that scope to an OAuth-minted
credential. Both are later decisions; neither changes the
token-is-a-credential property, which is the whole of this ADR.
