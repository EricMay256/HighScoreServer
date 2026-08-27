"""Agent credentials and scope checks for the vault surface.

Replaces the single shared ``VAULT_READ_API_KEY`` with the operator-issued
tokens the integration spec describes: ``hssv1_<credential-id>_<secret>``, where
the credential ID is a non-secret lookup key and only the SHA-256 of the secret
is stored. A leaked database gives an attacker hashes, not tokens.

This is deliberately a static-bearer profile, not OAuth. The clients are known,
controlled agents and HSS has no authorization server. If a client ever needs
the MCP authorization flow, that is a different design and not a tweak to this
one.
"""

import hmac
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256


logger = logging.getLogger(__name__)

TOKEN_PREFIX = "hssv1"

# Mirrors vault_agent_credentials_id_format. Note the id may contain '_', which
# is why the token is split from the right rather than the left.
_CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Secrets are hex at issuance precisely so this is unambiguous: the id may hold
# underscores, the secret may not, so the final '_' is always the separator.
_SECRET_PATTERN = re.compile(r"^[0-9a-f]{32,128}$")

# Compared against when no credential row matched, so that an unknown ID costs
# the same work as a wrong secret. Without it, response timing distinguishes
# "no such credential" from "bad secret", which enumerates valid IDs.
_DUMMY_HASH = sha256(b"vault-credential-miss").digest()


class VaultScope:
    """The scopes the schema's CHECK constraint permits.

    Scopes are verbs, per ADR 0015, and since 0020 the three write verbs are
    separate: ``WRITE`` means *contribute* alone. It used to gate replacement
    and deletion as well, which made "may add a note" and "may destroy one" the
    same grant — and the importer, the only long-lived credential, held it.

    ``WRITE`` also covers ADR 0035's summary carveout, which is the one place a
    second route shares a verb. The test that permits it is not "the route is
    small": it is that the route grants no capability the verb did not already
    carry. A contributor can put any summary it likes on its own note at
    contribute time, so the carveout adds a later *moment* to do that and not a
    new power — a `vault:summarize` could never be usefully withheld from a
    contributor, nor usefully granted without one. Anything that can reach a
    field its holder could not already have written needs its own verb.

    ``DELETE`` rather than ``RETIRE`` even though the route, the service and the
    quota bucket all say retire. ADR 0019's point is that retiring *is* deletion
    with no archived row left behind, and the audience for a scope name is an
    operator deciding whether to hand it over. "Retire" reads as reversible.
    """

    READ = "vault:read"
    WRITE = "vault:write"
    PROPOSE = "vault:propose"
    UPDATE = "vault:update"
    DELETE = "vault:delete"
    REVIEW = "vault:review"
    COMPILE = "vault:compile"
    EXPORT = "vault:export"


@dataclass(frozen=True, slots=True)
class VaultCredential:
    """A stored credential, without its secret."""

    id: str
    principal_id: str
    display_name: str
    secret_sha256: bytes
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= moment)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True, slots=True)
class ParsedToken:
    credential_id: str
    secret: str


def parse_token(token: str) -> ParsedToken | None:
    """Split a bearer token, or None if it is not even the right shape.

    Shape failures are indistinguishable from wrong secrets to the caller: both
    end in 401. Separating them here only keeps the lookup from running on
    input that cannot match anything.
    """

    prefix, separator, rest = token.partition("_")
    if not separator or prefix != TOKEN_PREFIX:
        return None

    credential_id, separator, secret = rest.rpartition("_")
    if not separator:
        return None
    if not _CREDENTIAL_ID_PATTERN.fullmatch(credential_id):
        return None
    if not _SECRET_PATTERN.fullmatch(secret):
        return None
    return ParsedToken(credential_id=credential_id, secret=secret)


def hash_secret(secret: str) -> bytes:
    """The 32 bytes stored for a secret.

    Plain SHA-256 rather than a password KDF on purpose: the secret is
    machine-generated with full entropy, so there is no dictionary to slow
    down, and a read surface cannot afford a deliberately slow hash per
    request. This reasoning does not transfer to human-chosen passwords.
    """

    return sha256(secret.encode("utf-8")).digest()


def secret_matches(credential: VaultCredential | None, secret: str) -> bool:
    """Constant-time secret comparison that also runs on a lookup miss."""

    presented = hash_secret(secret)
    expected = credential.secret_sha256 if credential else _DUMMY_HASH
    matched = hmac.compare_digest(presented, expected)
    return matched and credential is not None


def authorize(
    credential: VaultCredential | None,
    secret: str,
    required_scopes: Sequence[str],
    now: datetime | None = None,
) -> str | None:
    """Return a failure reason, or None when the request is authorized.

    The reason is for logging and for choosing 401 vs 403. It is never returned
    to the caller: "which of these was wrong" is not something an unauthorized
    client is owed.
    """

    # secret_matches runs the dummy comparison on a miss, so the timing is
    # already equalized by the time this returns; the None check below is for
    # the type checker, not for security, and must not short-circuit above it.
    matched = secret_matches(credential, secret)
    if credential is None or not matched:
        return "invalid"
    if not credential.is_active(now):
        return "inactive"
    missing = [scope for scope in required_scopes if not credential.has_scope(scope)]
    if missing:
        return "scope"
    return None
