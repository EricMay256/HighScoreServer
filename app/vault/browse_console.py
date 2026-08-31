"""The browse console: one page for reading the vault as a human.

The review console (ADR 0037) gave a person a way to *adjudicate* what agents
propose. It gave them no way to read the corpus, and it deliberately cannot
give them a way to propose: `vault:review` may be granted only to a family
holding `vault:read` alone, so the reviewer credential cannot also hold
`vault:propose`. Reading the vault therefore meant an export chore into some
other reader, which is enough friction that the corpus is mostly read by agents
and mostly written by them. See ADR 0039.

**It needs no operator grant.** `vault:read` and `vault:propose` are both in
`OAUTH_BASELINE_SCOPES`, so this page authorizes through the ordinary flow:
sign in and work. None of the entitlement machinery the reviewer needs applies
here. That inverts the expected order -- the surface that *writes* is cheaper
to authorize than the one that reads and decides -- and it is correct: a
proposal is inert until a reviewer applies it, and a decision is not.

**It asks for `vault:propose` before it can propose.** Reading is all this page
does today; inline span edits are the next step. The scope is requested now
because consent is what fixes a family's `authorized_scopes`: adding a scope
later means a fresh authorization, a fresh family, and an operator wondering
why a second one appeared. Asking once costs nothing -- the propose scope
grants no ability to change a note, only to queue a suggestion.

**A separate console is a separate credential, deliberately.** One page holding
both scope sets is impossible as scoped and undesirable if it were: the
separation guard exists precisely so a credential that can apply a change
cannot also author one. Two consoles, two families, one queue between them.

The OAuth lifecycle is not in this page. It lives in
``templates/_console_session.js`` and is configured by the values below, of
which ``STORE_PREFIX`` is the load-bearing one: sharing the reviewer's
namespace would have two pages writing one session record and presenting each
other's refresh tokens, which the authorization server reads as a captured
credential and answers by burning the family.
"""

import logging

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .console_page import API_BASE, console_page


logger = logging.getLogger(__name__)

BROWSE_PATH = "/vault/browse"

# Read to browse, propose to suggest. Space-separated because that is the OAuth
# scope syntax, and both are baseline -- see the module docstring for why the
# second one is requested before there is anything here that uses it.
CONSOLE_SCOPES = "vault:read vault:propose"

# Unverified free text like any client's name, so it decides nothing: the
# principal comes from the server-issued registration id (ADR 0024), and the
# operator's own name for the authorization is its label (ADR 0040).
CLIENT_NAME = "Vault browse console"

# This console's own storage namespace and refresh lock. Distinct from the
# reviewer's, which is the whole reason the session module takes it as config.
STORE_PREFIX = "vault.browse"

__all__ = [
    "API_BASE",
    "BROWSE_PATH",
    "CLIENT_NAME",
    "CONSOLE_SCOPES",
    "STORE_PREFIX",
    "browse_console",
    "build_vault_browse_routes",
]


async def browse_console(request: Request) -> HTMLResponse:
    """Serve the console shell. Carries no data and requires no credential."""

    return console_page(
        "browse.html",
        console_path=BROWSE_PATH,
        scopes=CONSOLE_SCOPES,
        client_name=CLIENT_NAME,
        store_prefix=STORE_PREFIX,
    )


def build_vault_browse_routes() -> list[Route]:
    """The console's routes, for the host to extend its router with."""

    return [Route(BROWSE_PATH, endpoint=browse_console, methods=["GET"])]
