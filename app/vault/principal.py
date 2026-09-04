"""Transport-neutral credential resolution and quota charging.

Two adapters now front the same services -- the HTTP routes in ``routes.py``
and the MCP tool surface in ``mcp.py`` -- and both need the same three steps
before any work happens: parse a bearer token, verify it against
``vault_agent_credentials``, and charge the principal's quota for the
operation.

None of those steps is HTTP-specific, but the original implementation expressed
all of them in FastAPI's vocabulary: it accepted an
``HTTPAuthorizationCredentials`` that only FastAPI's dependency system
constructs, and signalled failure by raising ``HTTPException``. An MCP tool call
has neither. There is no dependency injection in that path, and no HTTP
response to carry a status code or a ``Retry-After`` header, because MCP speaks
JSON-RPC.

So the shared logic lives here in terms of a plain token string and vault-owned
errors, and each adapter renders those errors in its own vocabulary: the routes
map them to 401/403/429 with a ``Retry-After`` header, the MCP adapter to
JSON-RPC errors an agent can read as text. The information carried is identical;
only the rendering differs. In particular this module discloses no more than the
routes ever did -- ``authorize`` still refuses to say *which* check failed, and
the token itself is still never logged.

**This is also where an OAuth arm attaches.** ``resolve_credential`` is
deliberately shaped like the MCP SDK's ``TokenVerifier`` protocol -- a token
string in, scopes out -- so replacing static bearer verification with token
introspection changes this module and nothing downstream. Scopes, quotas,
``contributed_by``, and ADR 0014's path policy all stay exactly where they are.

The connection discipline recorded in ``docs/archive/HANDOFF-2026-08-16.md``
(task 15) is preserved:
resolution takes its own short checkout and releases it before returning. It
must never hold one across the caller's work, because ``search``, ``contribute``
and ``update`` all call the embedding provider between their checkouts and would
otherwise pin a pooled connection across a 23-second worst case.
"""

import logging
from collections.abc import Sequence

from .auth import VaultCredential, authorize, parse_token
from .db import get_vault_engine
from .rate_limit import get_limiter
from .repository import VaultAgentCredentialRepository
from .service import VaultTransactionService


logger = logging.getLogger(__name__)


class VaultPrincipalError(Exception):
    """Base for the failures every vault adapter has to render."""


class VaultAuthError(VaultPrincipalError):
    """The token was absent, malformed, unknown, or inactive.

    One error for all four on purpose. Which check failed is not something an
    unauthorized caller is owed, and separating them here would eventually leak
    the distinction into a message.
    """


class VaultScopeError(VaultPrincipalError):
    """A valid, active credential that lacks a scope the operation requires."""


class VaultQuotaExceeded(VaultPrincipalError):
    """The principal's bucket for this operation is empty.

    Carries the wait as data rather than as a formatted header, because the two
    adapters render it differently and only one of them has headers.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__("Rate limit exceeded")
        self.retry_after = retry_after


async def resolve_credential(
    token: str | None,
    required_scopes: Sequence[str],
) -> VaultCredential:
    """Verify a bearer token and its scopes, or raise.

    ``token`` is the raw credential with no scheme prefix -- the caller has
    already stripped ``Bearer ``. ``None`` is accepted rather than rejected by
    the type system because "no credential offered" is an ordinary request shape
    that both adapters see, and it must cost the same as a wrong one.
    """

    parsed = parse_token(token) if token else None
    if parsed is None:
        # Shape failures skip the lookup because they cannot match anything.
        # They are indistinguishable from a wrong secret to the caller.
        raise VaultAuthError("Invalid vault credentials")

    repository = VaultAgentCredentialRepository()
    transactions = VaultTransactionService(get_vault_engine())
    async with transactions.transaction() as connection:
        credential = await repository.get(connection, parsed.credential_id)
        failure = authorize(credential, parsed.secret, required_scopes)
        if failure is None and credential is not None:
            await repository.touch(connection, credential.id)

    if failure == "scope" and credential is not None:
        # Never log the token; the credential ID is the non-secret half and is
        # what an operator needs to find the row.
        logger.warning(
            "Vault credential lacks a required scope",
            extra={
                "credential_id": credential.id,
                "principal_id": credential.principal_id,
                "required_scopes": list(required_scopes),
            },
        )
        raise VaultScopeError("Credential lacks the required scope")
    if failure is not None:
        raise VaultAuthError("Invalid vault credentials")
    if credential is None:  # unreachable; authorize() returns "invalid" first
        raise VaultAuthError("Invalid vault credentials")
    return credential


async def charge_quota(credential: VaultCredential, operation: str) -> None:
    """Charge one request against the principal's quota for this operation.

    ``operation`` must be registered in ``rate_limit.LIMITS``; the limiter
    raises ``ValueError`` otherwise, deliberately, so a new operation without a
    considered quota is a programming error rather than an unlimited bucket.
    Both adapters use the same operation names for exactly this reason -- a
    separate ``mcp.search`` bucket would let the same principal spend the
    quota twice by changing transport.
    """

    retry_after = await get_limiter().check(credential.principal_id, operation)
    if retry_after is None:
        return
    logger.warning(
        "Vault rate limit exceeded",
        extra={
            "principal_id": credential.principal_id,
            "operation": operation,
            "retry_after": retry_after,
        },
    )
    raise VaultQuotaExceeded(retry_after)
