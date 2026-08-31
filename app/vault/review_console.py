"""The operator's review console: one browser page, served by the vault.

The vault is an agent-facing service and this is its only human surface besides
the OAuth consent screen. It exists because reviewing is the one governed
operation a person must do and an agent must not: `vault:propose` writes inert
suggestions, `vault:review` applies them, and ADR 0021 keeps those apart on
purpose. Until this page there was no way to exercise the reviewing half except
by hand-writing HTTP calls, which is why proposals accumulated unreviewed.

**It authenticates as an ordinary OAuth client, not as an operator session.**
The vault already runs a full authorization server with PKCE, so the page takes
the same path any other client does: register, authorize, exchange, and carry a
scoped one-hour access token. The alternative -- a cookie session gated on
`VAULT_OPERATOR_PASSWORD_HASH` -- was rejected because it would let a browser
act with implicit privilege instead of a granted scope, which is precisely the
escalation `OAUTH_OPERATOR_ENTITLEMENT_SCOPES` exists to prevent. See ADR 0037.

**The page requests `vault:read` and nothing else.** That is not minimalism for
its own sake: `vault:review` may be granted only to a family holding
`vault:read` alone, so a page that also asked for `vault:write` would make
itself permanently ineligible for the entitlement it needs. The scope request
here and the separation-of-duties rule in the runbook are one decision, and
changing this line without changing that rule breaks the console.

Serving the page requires no scope. Reading or deciding anything requires the
token, checked by the API as it is for every other client -- so an unauthorized
visitor gets an empty shell and a sign-in button, never data.

**The OAuth lifecycle itself is not in this page.** It lives in
``templates/_console_session.js``, included into ``review.html``'s own script
and configured by the five values rendered below. That module is the expensive
part -- cross-tab rotation, the persisted record, the settled resume, a startup
sequence a test can drive -- and a second console (ADR 0039) that copied it
would copy every defect found in it so far. This module owns the page, the
scopes it asks for, and the response headers; the session module owns how it
holds a credential.
"""

import logging

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .templating import render


logger = logging.getLogger(__name__)

REVIEW_PATH = "/vault/review"

# The API the page talks to. Same origin, so the page needs no issuer URL: the
# authorization server, the resource server, and this page are one deployment.
API_BASE = "/api/v1/vault"

# What the page asks for. See the module docstring -- this is load-bearing.
CONSOLE_SCOPES = "vault:read"

# How this console names itself at registration. Unverified free text like any
# client's, so it decides nothing: the principal is derived from the
# server-issued registration id (ADR 0024), and the operator's own name for the
# authorization is its label (ADR 0040).
CLIENT_NAME = "Vault review console"

# Namespace for this console's browser storage and its refresh lock. A second
# console must not share it: two pages writing one session record would present
# each other's refresh tokens, which the authorization server reads as a
# captured credential and answers by burning the family.
#
# The value is also load-bearing backwards. The session-scoped format this
# replaced wrote `vault.review.client_id` and `vault.review.refresh`, and the
# shared module derives those legacy keys from this prefix -- so changing it
# would silently drop every live reviewer's session and cost a re-grant.
STORE_PREFIX = "vault.review"


async def review_console(request: Request) -> HTMLResponse:
    """Serve the console shell. Carries no data and requires no credential."""

    body = render(
        "review.html",
        api_base=API_BASE,
        scopes=CONSOLE_SCOPES,
        console_path=REVIEW_PATH,
        client_name=CLIENT_NAME,
        store_prefix=STORE_PREFIX,
    )
    return HTMLResponse(
        body,
        headers={
            # A review console that can be framed is a decision the operator
            # did not intend to make -- the same reasoning as the consent
            # screen, and the same treatment.
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'; "
                "base-uri 'none'; "
                "form-action 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


def build_vault_review_routes() -> list[Route]:
    """The console's routes, for the host to extend its router with.

    Separate from ``build_vault_oauth_routes`` because this is a different
    concern that happens to share a deployment: OAuth owns authorization, this
    owns a page. They are mounted together only because both need the vault to
    be publicly reachable.
    """

    return [Route(REVIEW_PATH, endpoint=review_console, methods=["GET"])]
