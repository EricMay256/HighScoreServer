# 15. Operator-issued agent credentials replace the shared read key

Date: 2026-07-29

## Status

Accepted. Replaces the `VAULT_READ_API_KEY` mechanism described in ADR 0008's context and in
the read-only slice's configuration runbook.

**Note, 2026-08-14 — the Context's aside on slowapi is superseded, the decision is not.** It
records that "slowapi lives in the host package and is unreachable from `app/vault/`", which
was the reasoning for a vault-local token bucket. That bucket exists and remains the
per-principal quota. But the constraint as stated was too broad: slowapi is a *third-party*
package, and the isolation rule forbids importing the **host**, not importing what the host
also happens to use. `app/vault/rate_limit.py` now builds its own independent `Limiter` for an
IP-keyed pre-authentication guard, which the per-principal quota structurally cannot provide —
a quota keyed on the credential cannot charge the lookup that resolves the credential. Nothing
about operator-issued credentials changes. See `vault-configuration.md`.

## Context

The read-only slice gated access on one shared secret in the environment. Its own module
docstring called that a placeholder — "when the vault gains real agent credentials, this is
the seam they replace" — and the integration spec had already specified the replacement in
detail. So this is executing a decision rather than making one; what follows records the
parts that were genuinely open.

A single shared secret has no notion of *who* is calling. Rotation means editing config and
restarting; revoking one consumer means revoking all of them; and every caller necessarily
holds every capability. That is tolerable for a surface with one operator and no writes, and
stops being tolerable the moment there is a second consumer or a write path — both of which
are now near.

`vault_agent_credentials` has existed since the first migration, unused, with exactly the
columns this needs.

## Decision

**Bearer tokens of the form `hssv1_<credential-id>_<secret>`, verified against
`vault_agent_credentials`, with `vault:read` required for the read surface.**

Only `sha256(secret)` is stored. A leaked database yields hashes, not tokens.

Four details that were decisions rather than transcription:

**The token is split from the right.** `vault_agent_credentials_id_format` permits `_` in a
credential ID, so splitting from the left would truncate any ID containing one and fail to
authenticate a valid credential. Secrets are therefore issued as hex, which makes the final
`_` unambiguously the separator. A test pins an underscore-bearing ID precisely because the
naive implementation passes every other case.

**A lookup miss still performs a comparison.** Returning early when no row matches would let
response timing enumerate valid credential IDs. `secret_matches` compares against a fixed
dummy hash instead, so an unknown ID costs what a wrong secret costs.

**Plain SHA-256, not a password KDF.** Secrets are machine-generated with full entropy, so
there is no dictionary for a slow hash to frustrate, and a read surface cannot afford a
deliberately expensive hash per request. This reasoning does **not** transfer to
human-chosen passwords, and the module says so where someone might copy it.

**`last_used_at` is written only on success.** The column means "last used", not "last
attempted"; updating it on a failed secret would let anyone holding a credential ID keep a
dormant credential looking live to an operator reviewing the list.

`401` and `403` are distinguished — a bad token is a client that cannot talk to us, a
missing scope is one we deliberately did not grant something — but neither response says
which check failed.

## Consequences

`VAULT_READ_API_KEY` is gone, along with the 503-when-unconfigured behaviour that existed to
stop an unset key serving the corpus anonymously. There is no global switch any more: a
credential either verifies or it does not. `.env.example` and the configuration runbook drop
the variable, and `scripts/issue_vault_credential.py` issues, lists, and revokes.

**Authentication now costs a database round trip**, where it used to be a string comparison.
That is inherent to per-principal credentials and is the price of revocation taking effect on
the next request with no cache to expire. The lookup and the `last_used_at` write share one
transaction. If this ever shows up in latency, a short-TTL cache is the lever — and it trades
away exactly that immediate revocation, so it should be a deliberate decision rather than an
optimization someone reaches for.

**Scopes are verbs, not content.** `vault:read` grants the whole readable corpus; what is
readable is ADR 0014's path policy, which is a property of the folder rather than of the
credential. A future need for per-credential path restrictions would be a new decision, and
the natural shape — a path allowlist column on the credential — is available but deliberately
not built.

**Rate limiting is still absent**, and this ADR makes it implementable for the first time:
the spec's limits are per authenticated principal, and until now there was no principal.
slowapi lives in the host package and is unreachable from `app/vault/`, so it needs either a
vault-local limiter or enforcement at the proxy. That remains the last thing standing between
this surface and something untrusted.

**`vault:write`, `vault:review`, `vault:compile`, and `vault:export` are recognised but
ungranted by any route**, because those surfaces do not exist. The scope constants and the
issuing script accept them so that building one of those routes is adding a route, not
retrofitting an authorization model.
