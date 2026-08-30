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


async def review_console(request: Request) -> HTMLResponse:
    """Serve the console shell. Carries no data and requires no credential."""

    body = render(
        "review.html",
        api_base=API_BASE,
        scopes=CONSOLE_SCOPES,
        review_path=REVIEW_PATH,
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
