"""A throwaway authorization server, built to answer one question.

ADR 0024 proposes that the vault host its own OAuth 2.1 authorization server so
clients that cannot send a static header — the claude.ai web connector — can
reach it. The mobile case is what makes that worth building, and it rests on a
fact nobody has checked: **whether the client performs connector authorization
in a system browser or an embedded webview.** Google refuses OAuth in an
embedded webview (`disallowed_useragent`), enforced on Google's side with no
setting that disables it, so the answer decides which identity method is
reachable from a phone.

This module exists to read that answer off a real request, and then be deleted.
It is deliberately *not* the provider ADR 0024 describes:

- **It authenticates nobody and issues nothing.** ``authorize`` refuses every
  request. There is no path here to a token, by construction.
- **It stores registered clients in memory**, so a client registered on one
  Gunicorn worker is unknown to the other. Fine for a hand-run test, useless for
  anything real.
- **Seven of the ten provider methods raise.** Nothing reaches them, because
  nothing gets past ``authorize``.

What it does do is log the ``User-Agent`` of anything that touches the
authorization endpoints, which is the whole point. That happens in middleware
rather than in ``authorize`` because the protocol hands ``authorize`` no request
object — and because middleware still sees a request that fails client lookup,
which a partially-working spike will do often.

**Inert unless ``VAULT_OAUTH_SPIKE_ENABLED`` is true.** That gate is not
ceremony: mounting these routes publishes OAuth discovery metadata, and ADR 0024
is explicit that advertising an authorization server before one answers is worse
than the current honest dead end. A deployment that has not opted in serves no
metadata and behaves exactly as it does today.
"""

import logging
import os
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl


logger = logging.getLogger(__name__)

# The scopes ADR 0024 sets as the baseline. Present so the metadata this spike
# publishes matches what the real server would, and a client that inspects it
# sees the truth rather than a placeholder.
BASELINE_SCOPES = ["vault:read", "vault:write"]


def spike_enabled() -> bool:
    """Whether to mount the spike at all.

    Parsed the same way ``vault_enabled`` parses its gate, and defaulting to
    off for the reason in the module docstring.
    """

    value = (os.environ.get("VAULT_OAUTH_SPIKE_ENABLED") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _probe(inner: Any, path: str) -> Any:
    """Wrap one route's ASGI app so it records the browser that reached it.

    The one output of this spike. It is an ASGI wrapper rather than middleware
    on a sub-application because these routes are added to the host app's router
    directly -- RFC 9728 and RFC 8414 fix the metadata paths relative to the
    host, so there is no sub-app to hang middleware on. A wrapper travels with
    the route wherever it is mounted.

    An embedded webview usually announces itself in the ``User-Agent`` -- ``wv``
    on Android, or a host application's name -- while a system browser looks
    like ordinary Chrome or Safari. The raw string is logged rather than a
    verdict, because the shapes change and the judgement belongs with whoever
    reads the log. ``Sec-Fetch-Dest`` and ``Referer`` come along because a UA
    alone is sometimes ambiguous.
    """

    async def probed(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers") or []
            }
            logger.info(
                "vault oauth spike request",
                extra={
                    "vault_oauth_path": path,
                    "vault_oauth_method": scope.get("method", ""),
                    "vault_oauth_user_agent": headers.get("user-agent", ""),
                    "vault_oauth_sec_fetch_dest": headers.get("sec-fetch-dest", ""),
                    "vault_oauth_referer": headers.get("referer", ""),
                },
            )
        await inner(scope, receive, send)

    probed.__name__ = _endpoint_name(path)
    probed.__module__ = __name__
    return probed


def _endpoint_name(path: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace(".", "_").replace("-", "_")
    return f"vault_oauth_spike_{cleaned or 'root'}"


class SpikeAuthorizationProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Enough of the protocol to be walked to ``authorize``, and no further."""

    def __init__(self) -> None:
        # In memory, and therefore per worker. See the module docstring: this is
        # why the spike is not a foundation to build on.
        self._clients: dict[str, OAuthClientInformationFull] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is None:
            # Worth logging rather than returning quietly: against two workers
            # this is the expected failure, and it looks like a client bug.
            logger.info(
                "vault oauth spike: unknown client (registered on another worker?)",
                extra={"vault_oauth_client_id": client_id},
            )
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        logger.info(
            "vault oauth spike: client registered",
            extra={
                "vault_oauth_client_id": client_info.client_id,
                "vault_oauth_redirect_uris": [
                    str(uri) for uri in client_info.redirect_uris or []
                ],
            },
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Refuse, having already been logged by the middleware.

        Raising ``AuthorizeError`` rather than returning a redirect is
        deliberate: the SDK renders it as a proper OAuth error the client can
        display, and there is no branch here that could ever reach a token.
        Stage 2 of the spike replaces this body with a redirect to Google and
        adds a callback route; nothing else in this module changes.
        """

        logger.info(
            "vault oauth spike: authorize reached",
            extra={
                "vault_oauth_client_id": client.client_id,
                "vault_oauth_redirect_uri": str(params.redirect_uri),
                "vault_oauth_scopes": list(params.scopes or []),
            },
        )
        raise AuthorizeError(
            error="temporarily_unavailable",
            error_description=(
                "The vault authorization server is a diagnostic stub and issues "
                "no tokens. Reaching this message means the discovery chain "
                "works and the User-Agent has been recorded."
            ),
        )

    async def load_authorization_code(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("spike: authorize never issues a code")

    async def exchange_authorization_code(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("spike: authorize never issues a code")

    async def load_refresh_token(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("spike: no tokens are issued")

    async def exchange_refresh_token(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("spike: no tokens are issued")

    async def load_access_token(self, token: str) -> None:
        raise NotImplementedError("spike: no tokens are issued")

    async def revoke_token(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("spike: no tokens are issued")


def resource_metadata_url(mcp_url: str) -> str:
    """Where a client is told to look for this resource server's metadata.

    RFC 9728 puts it under the host root with the resource's path appended, not
    beneath the resource itself -- so for ``/api/v1/vault/mcp`` it lands at
    ``/.well-known/oauth-protected-resource/api/v1/vault/mcp``. Computed by the
    SDK rather than by string-building, because getting it subtly wrong yields a
    404 that looks like the server simply not supporting OAuth.
    """

    return str(build_resource_metadata_url(AnyHttpUrl(mcp_url)))


def build_spike_routes(issuer_url: str, mcp_url: str) -> list[Any]:
    """Every route the discovery chain needs, for mounting at the host root.

    Three documents have to line up or a client gives up before it ever reaches
    ``/authorize``:

    1. the ``401`` from the MCP endpoint names the resource metadata URL,
    2. that metadata names the authorization server,
    3. the authorization server's own metadata names ``/authorize``.

    This function supplies 2 and 3. Step 1 lives with the MCP mount, which is
    the only place that issues the challenge.

    Root-mounted because RFC 9728 fixes the metadata path relative to the host,
    and because the SDK builds ``/authorize`` and friends from ``issuer_url`` --
    so issuer and routes have to agree about where they are.
    """

    routes = [
        *create_protected_resource_routes(
            resource_url=AnyHttpUrl(mcp_url),
            authorization_servers=[AnyHttpUrl(issuer_url)],
            scopes_supported=BASELINE_SCOPES,
        ),
        *create_auth_routes(
            provider=SpikeAuthorizationProvider(),
            issuer_url=AnyHttpUrl(issuer_url),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=BASELINE_SCOPES,
                default_scopes=BASELINE_SCOPES,
            ),
        ),
    ]
    return [_named(route) for route in routes]


def _named(route: Any) -> Any:
    """Install the User-Agent probe, and give slowapi a name to read.

    HSS's rate-limit middleware calls ``f"{handler.__module__}.{handler.__name__}"``
    on every route to decide exemption. The SDK wraps some auth endpoints in a
    ``CORSMiddleware`` instance, which has neither attribute, so a request to one
    raises ``AttributeError`` inside the middleware stack -- before the endpoint
    runs, and with a traceback naming slowapi rather than anything here. That was
    the first thing this spike found, which is a fair advertisement for spikes.
    """

    if getattr(route, "app", None) is not None:
        route.app = _probe(route.app, route.path)

    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None and not hasattr(endpoint, "__name__"):
        try:
            endpoint.__name__ = _endpoint_name(route.path)
            endpoint.__module__ = __name__
        except AttributeError:
            # A slotted callable would refuse the assignment. Better to let the
            # route through unlabelled and fail loudly at request time than to
            # swallow it here.
            logger.warning(
                "vault oauth spike: could not label endpoint for %s", route.path
            )
    return route
