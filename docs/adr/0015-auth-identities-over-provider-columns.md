# 15. Auth identities over provider columns

Date: 2026-07-10

## Status

Accepted

## Context

HSS originally stored the durable account and its proof mechanisms together in
`users`: `username`, nullable `email`, nullable `password_hash`, and `is_guest`.
That worked for guest accounts and a single native email/password login, but it
does not scale cleanly to external identity providers. Adding Steam as
`users.steam_id`, then Epic as `users.epic_id`, and so on would make providers
schema, not data, and would make account linking awkward.

The gameplay and leaderboard model already wants a stable account id. Scores,
runs, refresh tokens, guest pruning, and claimed-account gating all key off
`users.id`; they do not need to know whether a player proved identity through
email/password, Steam, Epic, or a future provider.

## Decision

Keep `users` as the canonical leaderboard account and add `auth_identities` as
the first-class set of authenticators attached to that account.

Each row stores:

- `user_id`: the canonical HSS account.
- `provider`: a lowercase provider key such as `ubear`, `steam`, or `epic`.
- `provider_user_id`: the provider's stable subject id, stored as text.

The unique constraint on `(provider, provider_user_id)` enforces that one
external account maps to exactly one HSS account. Multiple rows may point at the
same `user_id`, which is how a player links Steam, Epic, and the native `ubear`
email/password identity to one leaderboard identity.

Native register and claim flows create a `ubear` identity row in the same
transaction as the `users` update. External provider integrations must validate
the provider credential server-side first, then call the provider-neutral
resolve/link helpers with the verified subject id. For Steam, that means
validating a session ticket or equivalent backend-verifiable credential before
using the resulting SteamID64; the server must never trust a client-sent SteamID.

## Consequences

- New providers are data and auth-boundary code, not schema migrations.
- Downstream leaderboard behavior remains unchanged: once an identity resolves
  to `users.id`, JWTs, refresh tokens, scores, runs, and claimed-account gates
  continue to operate on the same canonical user.
- Guests can be upgraded in place by linking any durable identity row, not only
  by setting email/password fields.
- `provider_user_id` is `text`, even for SteamID64. This avoids unsigned-64-bit
  concerns and supports string subject ids from providers such as Epic or
  Google.
- The native email/password fields remain on `users` for compatibility. Moving
  password auth fully behind `auth_identities` would be a separate migration and
  is not required to support external providers.
