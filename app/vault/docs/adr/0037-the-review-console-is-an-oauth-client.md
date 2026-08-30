# 0037. The review console is an OAuth client, not an operator session

Date: 2026-08-29

## Status

Accepted.

## Context

The vault is an agent-facing service. Its only human surface was the OAuth
consent screen, which exists to approve a client rather than to do any work.

Reviewing is the one governed operation a person must perform and an agent must
not. ADR 0021 separates `vault:propose` from `vault:review` precisely so that an
agent may author inert suggestions and cannot apply them, and the runbook adds a
separation-of-duties rule on top: `vault:review` may be granted only to a family
holding `vault:read` alone.

The consequence went unnoticed until there was a queue. Forty-four metadata
proposals and five older ones accumulated with no way to adjudicate them except
hand-written HTTP calls. Worse, the payload a reviewer receives for the cheapest
change kind is a list of 32-character ids, and "do these two notes belong
together" is not a question anyone can answer from an id — ADR 0036 records
that gap and `metadata_preview` closes it, but a field nothing renders is still
not a review surface.

Three shapes were available.

## Decision

Serve a review console from the vault package at `/vault/review`, and have it
**authenticate as an ordinary OAuth client** — dynamic registration,
authorization code, PKCE, a one-hour scoped access token — exactly as any other
client does.

The page requests **`vault:read` and nothing else**. This is not minimalism: it
is the separation-of-duties rule expressed in code. A console that also asked
for `vault:write` would make its own family permanently ineligible for the
entitlement it exists to use, and would fail at the grant rather than at the
request. `CONSOLE_SCOPES` and that rule are one decision in two places, and a
test pins them together.

`vault:review` arrives the only way it can: an operator grants it to this
family with `issue_vault_credential grant-oauth`. The console therefore refuses
on first run by design, so it renders the exact command with its own credential
id parsed from its token — otherwise the refusal reads as a bug and the reason
is buried in a runbook section about scopes.

The console covers both queues `vault:review` governs: amendment proposals and
near-duplicate review cases. It renders them differently on purpose, because
their decisions are not symmetric — rejecting a proposal discards an inert
suggestion, while rejecting a review case **deletes** the candidate note. The
API calls both "rejected"; a surface that rendered them identically would be
inviting the mistake, so the deleting one carries a warning and a confirmation.

## Consequences

The console is a page, not an API. It consumes the endpoints that already exist
and adds no route that returns vault data, so the authorization story is
unchanged: an unauthenticated visitor gets an empty shell, and every byte of
content is fetched with a token the API checks as it would any client's.

It ships and leaves with the package. It imports nothing from the leaderboard,
and the host wires it in the same `VAULT_PUBLIC_URL` block that mounts the
authorization server — without a reachable issuer the page could render but
never sign in, so gating it on anything else would be a way to serve a console
that cannot work.

Bulk acceptance is offered for metadata proposals only, and is withheld from any
proposal whose preview reports removed body lines. Removal acknowledgement is a
deliberate per-proposal act (ADR 0019's amendment); a "select all" that could
satisfy it in aggregate would erase the point of requiring it.

## Alternatives considered

**A static HTML file calling the API with a pasted token.** This was the
starting suggestion. Rejected: it puts a long-lived `vault:review` credential in
a file and a clipboard, cannot participate in the OAuth session, and gains
nothing from being HTML — it is a console script with a stylesheet.

**A cookie session gated on `VAULT_OPERATOR_PASSWORD_HASH`.** The most obviously
convenient option, and the most dangerous. It would let a browser act with
implicit privilege rather than a granted scope, which is exactly the escalation
`OAUTH_OPERATOR_ENTITLEMENT_SCOPES` was separated from the baseline to prevent.
The operator password authorizes *a client*; it must not become a way to skip
having one.

**A Python operator script.** Legitimate, and rejected only on fit. It would
need the same dedicated `read`+`review` credential, and would put that token in
a shell history. The separation-of-duties rule already forces the reviewer
credential to be single-purpose and long-lived, which suits a browser session
and monthly reauthorization better than a terminal.
