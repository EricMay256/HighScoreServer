"""The operator login page, and the route assembly for the authorization server.

Three groups of routes, all mounted at the **host root** rather than under
``/api/v1/vault``: RFC 9728 fixes the protected-resource metadata path relative
to the host, and the SDK builds ``/authorize`` and friends from ``issuer_url``,
so issuer and routes have to agree about where they live.

- RFC 9728 protected-resource metadata, naming this authorization server.
- The SDK's own ``/authorize``, ``/token``, ``/register``, ``/revoke`` and RFC
  8414 metadata, over ``VaultAuthorizationProvider``.
- ``GET`` and ``POST /vault/login`` -- this module's only original HTTP, and the
  step ``authorize`` hands off to.

**Starlette routes, not a FastAPI router**, because the SDK returns Starlette
``Route`` objects and these have to sit beside them in one list the host
extends onto its router. Nothing here is in the OpenAPI schema, which is
correct: a login form is not an API.

**The login page is stateless.** The password is entered once per authorization
and there is no session cookie -- ADR 0024's decision, on the grounds that
authorizing a client is rare and a session would be a third credential type with
its own lifetime, storage and revocation story. Everything that has to survive
the round trip is in the pending-authorization row.

**One failure message, whatever failed.** A wrong password, an expired nonce, a
nonce that never existed, a bad CSRF token, and an unconfigured operator
password all render the same sentence. ADR 0015's rule about ``401`` versus
``403`` applied to a form: a page that distinguished them would hand an attacker
a probe for valid authorization attempts, and the operator loses nothing because
they know which of the two they just did.
"""

import hmac
import logging
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from mcp.server.auth.routes import (
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from sqlalchemy import text as sql_text
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .constants import OAUTH_BASELINE_SCOPES, OAUTH_CLIENT_LOCK_KEY
from .db import get_vault_engine
from .oauth import (
    LOGIN_PATH,
    NONCE_PARAM,
    VaultAuthorizationProvider,
    baseline_scopes,
    new_secret,
)
from .passwords import verify_password
from .rate_limit import (
    build_registration_guard,
    get_login_limiter,
    guard_asgi_app,
)
from .repository import (
    VaultOAuthAuthorizationCodeRepository,
    VaultOAuthClientRepository,
    VaultOAuthPendingAuthorizationRepository,
    hash_oauth_secret,
)
from .service import VaultTransactionService
from .settings import operator_password_hash
from .templating import render


logger = logging.getLogger(__name__)

# The single message every failure renders. Deliberately says nothing about
# which check failed.
FAILURE_MESSAGE = (
    "That did not work. Check the password and try the authorization again "
    "from the client."
)

# How much of a client's self-declared name the consent screen will show.
# Registration is open, so this string is attacker-supplied and unbounded; a
# name long enough to push the destination and the scopes out of view would
# undo the rest of the page.
MAX_DISPLAYED_CLIENT_NAME = 80

# What the consent screen says each scope means, in the operator's terms rather
# than the code's. Only the baseline scopes can appear here -- a client may not
# request more -- but the map covers every scope so that an operator-widened
# credential re-authorizing still renders something true.
SCOPE_DESCRIPTIONS: dict[str, str] = {
    "vault:read": "search the vault and read notes",
    "vault:write": "add new notes",
    "vault:update": "replace the contents of existing notes",
    "vault:delete": "permanently delete notes",
    "vault:review": "adjudicate flagged notes and promotion candidates",
    "vault:compile": "compile wiki pages from notes",
    "vault:export": "export the vault as markdown",
}

# Identifies which method authenticated the operator, recorded on the
# authorization code so an audit can tell a password login from a future Google
# one after the fact.
PASSWORD_SUBJECT = "operator:password"


def _transactions() -> VaultTransactionService:
    return VaultTransactionService(get_vault_engine())


def _describe(scopes: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "name": scope,
            "description": SCOPE_DESCRIPTIONS.get(
                scope, "an unrecognised permission -- do not approve this"
            ),
        }
        for scope in scopes
    ]


def _page(
    *,
    request_valid: bool,
    status_code: int = 200,
    client_name: str = "",
    client_id: str = "",
    redirect_origin: str = "",
    scopes: tuple[str, ...] = (),
    nonce: str = "",
    csrf_token: str = "",
    error: str | None = None,
) -> HTMLResponse:
    body = render(
        "login.html",
        request_valid=request_valid,
        client_name=client_name,
        client_id=client_id,
        redirect_origin=redirect_origin,
        scopes=_describe(scopes),
        nonce=nonce,
        csrf_token=csrf_token,
        nonce_param=NONCE_PARAM,
        login_path=LOGIN_PATH,
        error=error,
    )
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={
            # A consent screen that can be framed is a scope grant the operator
            # did not intend to make.
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
            # The page carries a nonce and a CSRF token in its markup, and the
            # URL carries them too. Neither belongs in a shared cache or in a
            # back-button replay.
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


async def _load_pending(nonce: str) -> tuple[Any, Any] | None:
    """The pending authorization and its client, or None if either is gone."""

    if not nonce:
        return None
    transactions = _transactions()
    async with transactions.transaction() as connection:
        pending = await VaultOAuthPendingAuthorizationRepository().peek(
            connection, nonce
        )
        if pending is None:
            return None
        client = await VaultOAuthClientRepository().get(connection, pending.client_id)
    return (pending, client) if client is not None else None


def _client_display_name(client: Any) -> str:
    """What the consent screen calls the client.

    Falls back to the id, and never to something reassuring: an unnamed client
    should look unnamed. Escaping is the template's job -- ``client_name`` is
    attacker-controlled, since registration is open.
    """

    info = client.client_info or {}
    name = str(info.get("client_name") or "").strip()
    # Truncated, because it is attacker-supplied and unbounded: a name long
    # enough to push the destination and the scope list off the operator's
    # screen would defeat the rest of this page. Escaping is the template's job.
    if len(name) > MAX_DISPLAYED_CLIENT_NAME:
        name = name[:MAX_DISPLAYED_CLIENT_NAME].rstrip() + "\u2026"
    return name or client.client_id


def _redirect_origin(pending: Any) -> str:
    """Where approving this would actually deliver the authorization code.

    The one part of a registration an attacker cannot borrow. ``client_name`` is
    free text on an open registration endpoint, so "Claude" on this page proves
    nothing -- anyone may register under that name, point the redirect at a host
    they control, and send the operator a genuine `/authorize` link on the real
    vault domain. The operator would see a trusted name, on the right site, with
    the right scopes, and the only thing distinguishing that request from the
    real one is the destination. So the destination is shown.

    Origin and not the full URI: the path cannot move the code to another host,
    and a long URI is a thing operators stop reading.
    """

    parsed = urlparse(str(pending.params.get("redirect_uri") or ""))
    if not parsed.scheme or not parsed.netloc:
        # Unreachable through `/authorize`, which validates the URI against the
        # registration before parking it. Shown rather than hidden if it ever
        # happens: a destination that cannot be described is not one to approve.
        return str(pending.params.get("redirect_uri") or "(none)")
    return f"{parsed.scheme}://{parsed.netloc}"


def _requested_scopes(pending: Any) -> tuple[str, ...]:
    """The scopes this authorization would grant.

    From the parked ``AuthorizationParams``, falling back to the baseline when
    a client requested none -- which is what ``ClientRegistrationOptions``
    would have given it anyway.
    """

    scopes = pending.params.get("scopes") or []
    return tuple(scopes) if scopes else tuple(OAUTH_BASELINE_SCOPES)


async def login_form(request: Request) -> Response:
    """Render the consent screen. Reads the nonce; does not spend it.

    A GET must not consume the pending authorization: an operator who reloads,
    or whose browser prefetches the link, would otherwise find their
    authorization already gone. Consumption belongs to the POST, which is the
    step that mints a code.
    """

    nonce = request.query_params.get(NONCE_PARAM, "")
    csrf_token = request.query_params.get("csrf", "")
    loaded = await _load_pending(nonce)
    if loaded is None:
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)

    pending, client = loaded
    return _page(
        request_valid=True,
        client_name=_client_display_name(client),
        client_id=client.client_id,
        redirect_origin=_redirect_origin(pending),
        scopes=_requested_scopes(pending),
        nonce=nonce,
        csrf_token=csrf_token,
    )


async def login_submit(request: Request) -> Response:
    """Verify the password, mint the code, and redirect back to the client.

    **The nonce is redeemed whatever the password turns out to be**, so one
    authorization is worth one attempt: a wrong guess burns it and the operator
    restarts from the client. That is the trade a public password endpoint
    wants, and it costs an honest operator a restart on a typo.

    The redemption and the code now share **one transaction**, which they did
    not until 2026-08-23, and the gap between them was a race. Redeeming
    committed on its own, bcrypt ran for a few hundred milliseconds, and only
    then was the code written -- and in that window an old registration has no
    pending authorization, no code, and no live refresh token, which is exactly
    the state stale-client pruning deletes. The operator typed the right
    password and got a 500 from a foreign key. Atomic, there is no committed
    moment when neither row exists, so the sweep always sees one of them; the
    advisory lock makes that explicit rather than incidental.

    The cost is that bcrypt now runs *before* redemption, so several requests
    arriving together on one nonce each get a password evaluation before one of
    them wins the redeem. The login bucket and bcrypt's own cost are what bound
    guessing, and they are unchanged -- `/authorize` mints nonces freely, so
    one-guess-per-nonce was never the thing stopping an attacker who wanted
    more.
    """

    form = await request.form()
    nonce = str(form.get(NONCE_PARAM) or "")
    submitted_csrf = str(form.get("csrf") or "")
    password = str(form.get("password") or "")

    loaded = await _load_pending(nonce)
    if loaded is None:
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)
    pending, client = loaded

    # CSRF: a server-side token rather than a signed one. Signing would need a
    # third secret to configure and rotate; a row already exists per
    # authorization, so the token lives there and is single-use for free. A
    # missing digest -- a row written before migration 0014 -- is refused, not
    # waved through.
    expected = pending.csrf_sha256
    if expected is None or not hmac.compare_digest(
        bytes(expected), hash_oauth_secret(submitted_csrf)
    ):
        logger.warning("vault oauth login rejected: csrf mismatch")
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)

    stored_hash = operator_password_hash()
    if stored_hash is None:
        # Unconfigured is a refusal, never "any password works" -- the same way
        # VAULT_ENABLED defaulting to false serves no vault rather than an
        # unguarded one. Logged loudly because it is an operator error the page
        # deliberately will not describe.
        logger.error(
            "vault oauth login attempted but VAULT_OPERATOR_PASSWORD_HASH is unset"
        )
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)

    # Outside the transaction below, deliberately: bcrypt is a deliberately slow
    # CPU-bound call and holding a pooled connection across one is the mistake
    # this package keeps naming.
    password_ok = await verify_password(password, stored_hash)

    transactions = _transactions()
    code = new_secret()
    async with transactions.transaction() as connection:
        await connection.execute(
            sql_text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": OAUTH_CLIENT_LOCK_KEY},
        )
        redeemed = await VaultOAuthPendingAuthorizationRepository().redeem(
            connection, nonce
        )
        if redeemed is not None and password_ok:
            params = redeemed.params
            await VaultOAuthAuthorizationCodeRepository().create(
                connection,
                code=code,
                client_id=redeemed.client_id,
                scopes=_requested_scopes(redeemed),
                code_challenge=params["code_challenge"],
                redirect_uri=str(params["redirect_uri"]),
                redirect_uri_provided_explicitly=bool(
                    params.get("redirect_uri_provided_explicitly", True)
                ),
                resource=params.get("resource"),
                subject=PASSWORD_SUBJECT,
            )

    if redeemed is None:
        # Raced by another submit, expired, or its registration was pruned --
        # which cascades the pending row away, so this covers that too.
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)

    if not password_ok:
        logger.warning("vault oauth login rejected: password mismatch")
        return _page(request_valid=False, status_code=400, error=FAILURE_MESSAGE)

    params = redeemed.params

    logger.info(
        "vault oauth authorization approved",
        extra={"client_id": redeemed.client_id},
    )
    return RedirectResponse(
        _client_redirect(params, code),
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def _client_redirect(params: dict[str, Any], code: str) -> str:
    """Where to send the browser once the code is minted.

    ``state`` is echoed **unmodified** or omitted entirely when the client sent
    none. A client that receives a changed or invented ``state`` rejects the
    response, and the failure surfaces at the client with no hint that it came
    from here.
    """

    query = {"code": code}
    state = params.get("state")
    if state is not None:
        query["state"] = state

    parsed = urlparse(str(params["redirect_uri"]))
    merged = f"{parsed.query}&{urlencode(query)}" if parsed.query else urlencode(query)
    return urlunparse(parsed._replace(query=merged))


def _endpoint_name(path: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace(".", "_").replace("-", "_")
    return f"vault_oauth_{cleaned or 'root'}"


def _guarded(route: Any, charge: Any = None) -> Any:
    """Charge the pre-auth IP guard before the route runs.

    Every route here is public and unauthenticated -- that is what an
    authorization server is -- and `/register` writes a row on each call. ADR
    0024 requires the guard to cover them and it did not: the guard is an
    ``APIRouter`` dependency on the vault router, and these are root-mounted
    Starlette routes that inherit nothing from it.

    Applied to ``route.app`` rather than as a decorator on the endpoint because
    the SDK's endpoints are ``CORSMiddleware`` instances, not
    ``async def(request)`` callables, so slowapi's decorator cannot wrap them.

    **A route whose endpoint already carries a slowapi bucket is left alone**,
    which today means the login POST. Wrapping it suppressed its own limiter
    entirely -- 15 attempts against a bucket of 10 all returned 400 and none
    returned 429 -- so the tighter, more important limit was traded for the
    looser one. The rule is therefore "one bucket per endpoint, the tightest
    that applies", and the login POST's 10/minute is strictly stronger than the
    600/minute guard it would otherwise inherit.
    """

    endpoint = getattr(route, "endpoint", None)
    if hasattr(endpoint, "__wrapped__"):
        return route
    inner = getattr(route, "app", None)
    if inner is not None:
        route.app = guard_asgi_app(inner, charge)
    return route


def _named(route: Any) -> Any:
    """Give slowapi a name to read on every route.

    The host's rate-limit middleware calls
    ``f"{handler.__module__}.{handler.__name__}"`` on each route to decide
    exemption. The SDK wraps some auth endpoints in a ``CORSMiddleware``
    instance, which has neither attribute, so a request to one raises
    ``AttributeError`` inside the middleware stack -- before the endpoint runs,
    and with a traceback naming slowapi rather than anything here. This was the
    first thing the 2026-08-22 spike found, and it is kept because the failure
    is invisible until the first real request.
    """

    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None and not hasattr(endpoint, "__name__"):
        try:
            endpoint.__name__ = _endpoint_name(route.path)
            endpoint.__module__ = __name__
        except AttributeError:
            # A slotted callable would refuse the assignment. Better to let the
            # route through unlabelled and fail loudly at request time than to
            # swallow it silently here.
            logger.warning(
                "vault oauth: could not label endpoint for %s", route.path
            )
    return route


def build_vault_oauth_routes(issuer_url: str, mcp_url: str) -> list[Route]:
    """Every route the OAuth flow needs, for mounting at the host root.

    Three documents have to line up or a client gives up before it ever reaches
    ``/authorize``:

    1. the ``401`` from the MCP endpoint points at the resource metadata,
    2. that metadata names this authorization server,
    3. the authorization server's metadata names ``/authorize``.

    Steps 2 and 3 are here. Step 1 needs no change: clients construct the
    well-known URL by convention, which the spike confirmed against the real
    client, so ``mcp.py``'s bare ``WWW-Authenticate: Bearer`` is sufficient.
    """

    provider = VaultAuthorizationProvider(_transactions)
    limiter = get_login_limiter()

    routes: list[Route] = [
        # **Both URL forms, and this is not belt-and-braces.** RFC 9728 derives
        # the metadata path from the resource path, so `.../mcp` and `.../mcp/`
        # yield two *different* well-known URLs. The mount answers only the
        # trailing-slash form and the operator docs tell people to register
        # that one, while the bare form is what an operator types and what the
        # 307 redirect exists for. Serving one and not the other 404s discovery
        # for whichever half a client happened to be configured with, and the
        # symptom is "this server does not support OAuth".
        #
        # Each document names the resource it actually describes rather than
        # pointing at a canonical one, so a client comparing `resource` against
        # what it asked for finds them equal.
        *create_protected_resource_routes(
            resource_url=AnyHttpUrl(mcp_url.rstrip("/")),
            authorization_servers=[AnyHttpUrl(issuer_url)],
            scopes_supported=baseline_scopes(),
        ),
        *create_protected_resource_routes(
            resource_url=AnyHttpUrl(mcp_url.rstrip("/") + "/"),
            authorization_servers=[AnyHttpUrl(issuer_url)],
            scopes_supported=baseline_scopes(),
        ),
        *create_auth_routes(
            provider=provider,
            issuer_url=AnyHttpUrl(issuer_url),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=baseline_scopes(),
                default_scopes=baseline_scopes(),
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        Route(LOGIN_PATH, endpoint=login_form, methods=["GET"]),
        # The POST carries its own bucket, far tighter than the 600/min pre-auth
        # guard. A public password endpoint is a brute-force target: bcrypt's
        # cost factor is the first defence and the IP guard the second, and
        # neither is sized for this. The GET is deliberately not limited --
        # rendering a form is cheap and reloading one is ordinary.
        Route(
            LOGIN_PATH,
            endpoint=limiter(login_submit),
            methods=["POST"],
        ),
    ]
    # `/register` is the one route here that writes a row on an
    # unauthenticated call, so it draws on its own far tighter bucket instead
    # of the general pre-auth allowance.
    registration_charge = build_registration_guard()
    return [
        _guarded(
            _named(route),
            registration_charge if getattr(route, "path", "") == "/register" else None,
        )
        for route in routes
    ]
