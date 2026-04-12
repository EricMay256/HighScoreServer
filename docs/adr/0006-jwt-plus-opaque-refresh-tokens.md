# 0006. JWT plus opaque refresh tokens

Date: 2026-04-12

## Status

Accepted

## Context

The application needs an authentication scheme that supports a Unity client running on player devices, where the client must be able to make authenticated API calls without prompting for credentials on every launch. The two pure approaches each fail in a specific way:

- **Pure JWT, no refresh.** Either tokens are short-lived and the player has to re-authenticate constantly (bad UX), or tokens are long-lived and there is no way to revoke a stolen token short of rotating the signing secret and invalidating every token at once.
- **Pure opaque tokens, server-side sessions.** Every authenticated request requires a database lookup to validate the token. This is correct but adds a round trip to every API call, which is wasteful for the common case where the token is valid.

The hybrid approach — short-lived JWT for the common case, opaque refresh token for the rare case — gets the cheap path of JWT validation (decode and verify signature, no DB hit) and the revocation surface of opaque tokens (the refresh token is a row in a table, deleting the row revokes it).

## Decision

Issue two tokens at every authentication event:

- **Access token.** A JWT, HS256, 60-minute lifetime. Validated on every authenticated request by signature check and expiry check, no database hit. The `sub` claim holds the user ID as a string (per RFC 7519, which requires `sub` to be a `StringOrURI`).
- **Refresh token.** A 32-byte cryptographically random string, returned to the client as a URL-safe base64 string and stored server-side as a SHA-256 hash in the `refresh_tokens` table. Used only against the `/api/auth/refresh` endpoint to obtain a new access token / refresh token pair.

Refresh tokens are single-use. Each refresh rotates to a new token and atomically deletes the old one via `DELETE ... RETURNING` in a single transaction. If two clients race to rotate the same token, exactly one `DELETE` returns a row and the other gets nothing — no read-then-write window where both could succeed. The losing client receives a 401 and falls back to guest login.

## Consequences

The common case is fast: an authenticated API call is one signature verification and no database round trip for auth. Only the refresh path touches the database, and only once an hour per active client.

Refresh tokens are stored as hashes, so a database read does not leak valid tokens. An attacker with read-only DB access cannot impersonate a user without also having the plaintext refresh token from the client side.

The known gap is access token revocation. A stolen access token is valid until its 60-minute expiry, and there is no server-side way to invalidate it within that window. The mitigation is the short lifetime itself: the blast radius of a stolen access token is bounded to one hour. The full fix is a JTI denylist — every access token carries a unique `jti` claim, and a check at decode time consults a fast store (Redis or similar) to see if that `jti` has been revoked. The decode-time check points are marked in the codebase with `# DENYLIST HOOK` comments so the implementation is a well-defined patch rather than an exploration.

The denylist is deferred deliberately. It requires a shared store that survives dyno restarts, which means provisioning Redis (see ADR 0007 for why Redis is currently not provisioned), and it adds a per-request cost that is only worth paying once there is a concrete reason to revoke tokens proactively — a known compromise, an account claim flow that wants to invalidate the guest's old tokens, or similar. None of those are present today.
