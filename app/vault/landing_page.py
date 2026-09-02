"""The vault's public front door.

The browse and review consoles are intentionally separate OAuth clients: one
can propose and the other can decide. The landing page is neither. It carries
no credential, fetches no corpus data, and gives a person the context needed to
choose the right console without weakening that separation.

Like both consoles it is gated on ``VAULT_PUBLIC_URL`` by the host. Without a
reachable authorization server, linking to pages that can never sign in would
be a misleading half-feature.
"""

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .console_page import vault_page


LANDING_PATH = "/vault"

__all__ = [
    "LANDING_PATH",
    "build_vault_landing_routes",
    "vault_landing",
]


async def vault_landing(request: Request) -> HTMLResponse:
    """Describe the vault and point to its two credentialed consoles."""

    return vault_page("landing.html")


def build_vault_landing_routes() -> list[Route]:
    """The landing route, for the host to register before its catch-all."""

    return [Route(LANDING_PATH, endpoint=vault_landing, methods=["GET"])]
