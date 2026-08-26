# 29. Operator-granted OAuth entitlements belong to the refresh family

Date: 2026-08-25

## Status

Accepted. Implements migration `0017_oauth_entitlements` and amends ADR 0024's
credential-level widening procedure.

## Context

ADR 0024 makes every OAuth access token an ordinary agent credential. That
keeps authentication, quotas, attribution, and tool filtering unified, but an
access credential rotates every hour. Adding `vault:update` or `vault:compile`
to that row therefore loses the operator's decision at the next refresh.

Copying widened scopes into the refresh token is not enough. It collapses two
different authorities into one array: scopes the client requested and the
operator consented to, and privileged scopes only an operator may grant. A
later refresh could neither distinguish nor safely revoke them. Attaching the
grant to the client registration instead is too broad: one registration can
hold several separately authorized sessions, and a compiler must not widen a
reviewer or ordinary contributor merely because they share a client id.

## Decision

Each OAuth authorization creates one durable `vault_oauth_grants` row keyed by
the refresh `family_id`. It stores two disjoint sets:

- `authorized_scopes` contains only OAuth-baseline scopes approved through consent;
- `entitled_scopes` contains only above-baseline scopes granted by the operator.

Every access credential and live refresh token is a projection of their union.
Refresh may narrow the authorized baseline but cannot add an entitlement or
remove one by echoing a different scope string. Each rotation recomputes the
union from the grant row, so an operator grant survives and an operator
revocation stays revoked.

The operator uses explicit family-aware commands:

```bash
python -m scripts.issue_vault_credential grant-oauth --id <credential-id> --scopes vault:compile
python -m scripts.issue_vault_credential revoke-oauth-scope --id <credential-id> --scopes vault:compile
```

The credential id is only a safe handle for finding its family. The command
prints the client and family before/after state, updates the live credential in
the same transaction, and records a durable operator audit event. The static
`grant` and `revoke-scope` commands refuse OAuth-minted credentials so an
operator cannot accidentally make an ephemeral change.

Entitlements are family-scoped, not registration-scoped. A new browser
authorization creates a new family with no inherited privilege. This makes
reauthorization a clean authority boundary and keeps separate roles separate
even when one connector registration owns them.

The reviewer rule from ADR 0026 is enforced by the operator command:
`vault:review` may be added only to a family whose effective set is exactly
`vault:read` plus `vault:review`. An ordinary read/write/propose session must
not be widened into a reviewer; authorize a separate read-only family first.

## Consequences

- Operator privilege survives access-token rotation without becoming
  client-requestable.
- Grant and revoke affect the current live credential immediately and all
  future credentials in that family.
- Revoking an entitlement narrows the tool surface but does not revoke the
  OAuth session. Token revocation still burns the family under ADR 0024.
- Reauthorization intentionally does not inherit entitlements. The operator
  must grant each new privileged family deliberately.
- Existing families are migrated with baseline scopes only and no entitlements.
  Historical one-token widening is not silently made permanent.
- Access credentials remain ordinary `vault_agent_credentials` rows. No second
  bearer-token type is introduced.
