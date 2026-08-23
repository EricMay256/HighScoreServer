"""The OAuth authorization server, driven end to end over HTTP.

Register, authorize, log in, redeem a code, use the resulting token against the
real vault surface, refresh it, and replay a rotated token. The point of doing
it through a client rather than by calling the provider is that most of the flow
is the SDK's -- PKCE, the ``redirect_uri`` round trip, code expiry, scope
validation -- and testing the provider alone would assert the half nobody was
worried about.

The property that matters most is the last one: **the access token this issues is
an ordinary ``hssv1_`` credential**, so it authenticates against the existing
vault routes with no OAuth-specific code anywhere in them. That is the whole of
ADR 0024, and it is asserted here rather than argued.
"""

import asyncio
import base64
import hashlib
import os
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.vault.auth import VaultScope
from app.vault.constants import OAUTH_BASELINE_SCOPES
from app.vault.oauth import LOGIN_PATH, PRINCIPAL_PREFIX
from app.vault.passwords import hash_password
from app.vault.rate_limit import reset_ip_limiter
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_agent_credentials,
    vault_oauth_authorization_codes,
    vault_oauth_clients,
    vault_oauth_pending_authorizations,
    vault_oauth_refresh_tokens,
)
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

OPERATOR_PASSWORD = "an operator password for the suite"
PUBLIC_URL = "https://vault.test.invalid"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture
def oauth_client(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The session client, with an operator password configured.

    **The session application, not one of this module's own**, and that is the
    load-bearing part. A second app entering its own lifespan tears the vault
    engine down for every other test in the run -- ``close_vault_db`` sets the
    module-level engine back to ``None`` -- while one that skips the lifespan
    instead inherits a connection pool bound to another event loop. Both are
    action at a distance in files this one never mentions; the first cost 28
    failures and the second an intermittent "Event loop is closed".

    ``tests/conftest.py`` therefore sets ``VAULT_PUBLIC_URL`` before
    ``app.main`` is imported, which is what puts the OAuth routes on the one
    application the suite shares. All this fixture adds is the operator
    password, which ``operator_password_hash`` reads per call and so can be set
    per test.
    """

    monkeypatch.setenv(
        "VAULT_OPERATOR_PASSWORD_HASH",
        asyncio.run(hash_password(OPERATOR_PASSWORD)),
    )
    reset_ip_limiter()
    yield client
    reset_ip_limiter()


def _cleanup() -> None:
    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(delete(vault_oauth_refresh_tokens))
                await connection.execute(delete(vault_oauth_authorization_codes))
                await connection.execute(delete(vault_oauth_pending_authorizations))
                await connection.execute(delete(vault_oauth_clients))
                await connection.execute(
                    delete(vault_agent_credentials).where(
                        vault_agent_credentials.c.principal_id.like(
                            f"{PRINCIPAL_PREFIX}%"
                        )
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())


@pytest.fixture(autouse=True)
def clean_oauth_state(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


# ------------------------------------------------------------- helpers ----


def register_full(client: TestClient, name: str = "Claude") -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def register(client: TestClient, name: str = "Claude") -> str:
    return register_full(client, name)["client_id"]


def authorize(client: TestClient, client_id: str, state: str = "opaque-state"):
    """Follow ``/authorize`` to the login page, returning its query parameters."""

    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": state,
            "scope": " ".join(OAUTH_BASELINE_SCOPES),
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)


def submit_login(client: TestClient, params: dict, password: str = OPERATOR_PASSWORD):
    return client.post(
        LOGIN_PATH,
        data={
            "req": params["req"][0],
            "csrf": params.get("csrf", [""])[0],
            "password": password,
        },
        follow_redirects=False,
    )


def exchange(client: TestClient, client_id: str, code: str) -> dict:
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def full_flow(client: TestClient, name: str = "Claude") -> tuple[str, dict]:
    """Register through to a token pair. Returns (client_id, token response)."""

    client_id = register(client, name)
    params = authorize(client, client_id)
    redirect = submit_login(client, params)
    assert redirect.status_code == 303, redirect.text
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
    return client_id, exchange(client, client_id, code)


# ------------------------------------------------------------ discovery ----


def test_the_discovery_chain_answers(oauth_client: TestClient) -> None:
    """Three documents have to line up or a client gives up before /authorize."""

    resource = oauth_client.get(
        "/.well-known/oauth-protected-resource/api/v1/vault/mcp"
    )
    server = oauth_client.get("/.well-known/oauth-authorization-server")

    assert resource.status_code == 200
    assert server.status_code == 200
    assert resource.json()["authorization_servers"] == [f"{PUBLIC_URL}/"]
    assert server.json()["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert set(server.json()["scopes_supported"]) == set(OAUTH_BASELINE_SCOPES)


def test_both_mcp_url_forms_have_protected_resource_metadata(
    oauth_client: TestClient,
) -> None:
    """RFC 9728 derives the metadata path from the resource path.

    So `.../mcp` and `.../mcp/` are two different well-known URLs, and the vault
    has reason to expect either: the mount answers only the trailing-slash form
    and the operator docs say to register that one, while the bare form is what
    an operator types and what the 307 redirect exists for. Serving one and not
    the other 404s discovery for whichever half a client was configured with,
    and the symptom is "this server does not support OAuth".

    Each document names the resource it describes, so a client comparing
    `resource` against what it requested finds them equal.
    """

    bare = oauth_client.get(
        "/.well-known/oauth-protected-resource/api/v1/vault/mcp"
    )
    slashed = oauth_client.get(
        "/.well-known/oauth-protected-resource/api/v1/vault/mcp/"
    )

    assert bare.status_code == 200
    assert slashed.status_code == 200
    assert bare.json()["resource"] == f"{PUBLIC_URL}/api/v1/vault/mcp"
    assert slashed.json()["resource"] == f"{PUBLIC_URL}/api/v1/vault/mcp/"
    for payload in (bare.json(), slashed.json()):
        assert payload["authorization_servers"] == [f"{PUBLIC_URL}/"]


def test_nothing_is_published_without_a_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence of ``VAULT_PUBLIC_URL`` is the off switch, and it fails closed.

    ADR 0024: advertising an authorization server before one answers is worse
    than the honest dead end of a bare 401. A deployment that cannot state its
    own origin cannot serve correct metadata, so it serves none.

    Asserted against the route table rather than over HTTP, because making
    requests would mean a second application -- which the ``oauth_client``
    docstring explains must not happen. The question here is only which routes
    ``create_app`` assembles, and the table answers it exactly.
    """

    monkeypatch.delenv("VAULT_PUBLIC_URL", raising=False)
    from app.main import create_app

    paths = {getattr(route, "path", "") for route in create_app().router.routes}

    assert "/authorize" not in paths
    assert "/token" not in paths
    assert LOGIN_PATH not in paths
    assert not any(path.startswith("/.well-known/oauth") for path in paths)


# ------------------------------------------------------- the happy path ----


def test_a_full_authorization_issues_a_usable_vault_credential(
    oauth_client: TestClient,
) -> None:
    """The assertion ADR 0024 exists for.

    The access token is an ordinary ``hssv1_`` credential, so it authenticates
    against the vault's existing routes with no OAuth-specific code in them.
    """

    _, tokens = full_flow(oauth_client)

    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("hssv1_")
    assert tokens["refresh_token"]
    assert set(tokens["scope"].split()) == set(OAUTH_BASELINE_SCOPES)

    search = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "anything"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert search.status_code == 200


def test_the_credential_carries_only_the_baseline_scopes(
    oauth_client: TestClient,
) -> None:
    """`vault:update`, `vault:delete` and `vault:review` are unreachable by request.

    A security decision, not a convenience one: ADR 0021's defence against text
    injected into the corpus is that a destructive tool is absent from the
    surface that text can name.
    """

    full_flow(oauth_client)

    assert _minted_scopes() == [sorted(OAUTH_BASELINE_SCOPES)]


def test_a_client_cannot_request_above_the_baseline(
    oauth_client: TestClient,
) -> None:
    client_id = register(oauth_client)

    response = oauth_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "scope": "vault:read vault:delete",
        },
        follow_redirects=False,
    )

    # Refused by construction rather than by an operator noticing on a screen.
    assert response.status_code == 302
    assert "error=invalid_scope" in response.headers["location"]


def test_state_is_echoed_unmodified(oauth_client: TestClient) -> None:
    """A client that receives a changed or invented ``state`` rejects the response."""

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id, state="a b&c=d")

    redirect = submit_login(oauth_client, params)

    returned = parse_qs(urlparse(redirect.headers["location"]).query)
    assert returned["state"] == ["a b&c=d"]


def test_the_consent_screen_names_the_client_and_its_scopes(
    oauth_client: TestClient,
) -> None:
    """A scope grant the operator never sees is one they did not make."""

    client_id = register(oauth_client, name="Claude Web")
    params = authorize(oauth_client, client_id)

    page = oauth_client.get(
        LOGIN_PATH, params={"req": params["req"][0], "csrf": params["csrf"][0]}
    )

    assert page.status_code == 200
    assert "Claude Web" in page.text
    for scope in OAUTH_BASELINE_SCOPES:
        assert scope in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Cache-Control"] == "no-store"


def test_a_client_name_carrying_markup_is_escaped(
    oauth_client: TestClient,
) -> None:
    """Registration is open, so ``client_name`` is attacker-controlled.

    This is the reason the page uses a template engine rather than string
    building: it renders next to a password field.
    """

    client_id = register(oauth_client, name="<script>alert(1)</script>")
    params = authorize(oauth_client, client_id)

    page = oauth_client.get(
        LOGIN_PATH, params={"req": params["req"][0], "csrf": params["csrf"][0]}
    )

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


# ------------------------------------------------------------ refusals ----


def test_a_wrong_password_and_a_bad_nonce_render_the_same_message(
    oauth_client: TestClient,
) -> None:
    """ADR 0024: one message, whatever failed.

    Distinguishing them would hand an attacker a probe for valid authorization
    attempts.
    """

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)
    wrong_password = submit_login(oauth_client, params, password="not it")

    params2 = authorize(oauth_client, client_id)
    unknown_nonce = submit_login(
        oauth_client, {"req": [uuid4().hex], "csrf": params2["csrf"]}
    )

    assert wrong_password.status_code == unknown_nonce.status_code == 400
    assert wrong_password.text == unknown_nonce.text


def test_a_missing_csrf_token_is_refused(oauth_client: TestClient) -> None:
    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)

    response = submit_login(oauth_client, {"req": params["req"], "csrf": [""]})

    assert response.status_code == 400
    assert _minted_scopes() == []


def test_a_wrong_password_burns_the_authorization(
    oauth_client: TestClient,
) -> None:
    """The nonce is redeemed before the password is checked, deliberately.

    One authorization affords exactly one attempt, so a live request cannot be
    used as an unlimited guessing oracle. The honest operator restarts from the
    client after a typo, which is the right trade for a public password form.
    """

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)

    submit_login(oauth_client, params, password="not it")
    retry = submit_login(oauth_client, params)

    assert retry.status_code == 400
    assert _minted_scopes() == []


def test_the_login_get_does_not_spend_the_nonce(oauth_client: TestClient) -> None:
    """A reload, or a browser prefetch, must not destroy the authorization."""

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)
    query = {"req": params["req"][0], "csrf": params["csrf"][0]}

    assert oauth_client.get(LOGIN_PATH, params=query).status_code == 200
    assert oauth_client.get(LOGIN_PATH, params=query).status_code == 200
    assert submit_login(oauth_client, params).status_code == 303


def test_login_refuses_when_no_operator_password_is_configured(
    oauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset never means "any password works"."""

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)
    monkeypatch.delenv("VAULT_OPERATOR_PASSWORD_HASH", raising=False)

    response = submit_login(oauth_client, params)

    assert response.status_code == 400
    assert _minted_scopes() == []


def test_an_authorization_code_cannot_be_redeemed_twice(
    oauth_client: TestClient,
) -> None:
    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)
    redirect = submit_login(oauth_client, params)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]

    exchange(oauth_client, client_id, code)
    second = oauth_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )

    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


def test_a_wrong_pkce_verifier_is_refused(oauth_client: TestClient) -> None:
    """Verified by the SDK, asserted here so a provider change cannot lose it."""

    client_id = register(oauth_client)
    params = authorize(oauth_client, client_id)
    redirect = submit_login(oauth_client, params)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]

    response = oauth_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": "b" * 64,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


# ------------------------------------------------------------- refresh ----


def _refresh(client: TestClient, client_id: str, refresh_token: str):
    return client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def test_refreshing_rotates_both_halves_and_revokes_the_old_credential(
    oauth_client: TestClient,
) -> None:
    client_id, tokens = full_flow(oauth_client)

    response = _refresh(oauth_client, client_id, tokens["refresh_token"])
    renewed = response.json()

    assert response.status_code == 200, response.text
    assert renewed["access_token"] != tokens["access_token"]
    assert renewed["refresh_token"] != tokens["refresh_token"]

    # The old access token stops working; the new one works.
    old = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    new = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {renewed['access_token']}"},
    )
    assert old.status_code == 401
    assert new.status_code == 200


def test_replaying_a_rotated_refresh_token_burns_the_whole_family(
    oauth_client: TestClient,
) -> None:
    """OAuth 2.1's replay detection, and the reason this table marks rather
    than deletes.

    Presenting a consumed refresh token is evidence a token was captured. Which
    party holds which is unknowable from the server, so the answer is to revoke
    the chain: the honest client re-authorizes, the attacker gets nothing.
    """

    client_id, first = full_flow(oauth_client)
    second = _refresh(oauth_client, client_id, first["refresh_token"]).json()

    replay = _refresh(oauth_client, client_id, first["refresh_token"])

    assert replay.status_code == 400
    # The replacement is dead too -- that is what "family" means.
    assert _refresh(oauth_client, client_id, second["refresh_token"]).status_code == 400
    still_live = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert still_live.status_code == 401


def test_a_refresh_may_narrow_scopes_but_not_widen_them(
    oauth_client: TestClient,
) -> None:
    client_id, tokens = full_flow(oauth_client)

    narrowed = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
            "scope": "vault:read",
        },
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["scope"] == "vault:read"

    widened = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": narrowed.json()["refresh_token"],
            "client_id": client_id,
            "scope": "vault:read vault:delete",
        },
    )
    assert widened.status_code == 400


def test_revoking_a_refresh_token_kills_its_access_token(
    oauth_client: TestClient,
) -> None:
    """RFC 7009: revoking a refresh token invalidates what it produced."""

    registration = register_full(oauth_client)
    client_id = registration["client_id"]
    params = authorize(oauth_client, client_id)
    redirect = submit_login(oauth_client, params)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
    tokens = exchange(oauth_client, client_id, code)

    revoked = oauth_client.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": client_id,
            # Required by `RevocationRequest` in mcp==2.0.0 even for a public
            # client, which declares `token_endpoint_auth_method: none` and has
            # no secret to send. An SDK quirk rather than a vault rule; sending
            # what registration returned is what a client would do. Operator
            # revocation does not go through here at all -- it is
            # `issue_vault_credential revoke` -- so this endpoint being awkward
            # for public clients costs the vault nothing.
            "client_secret": registration.get("client_secret") or "",
        },
    )

    assert revoked.status_code == 200
    used = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert used.status_code == 401


# ---------------------------------------------------------- rate limit ----


def test_the_login_post_has_its_own_tight_bucket(
    oauth_client: TestClient,
) -> None:
    """A public password endpoint is a brute-force target.

    bcrypt's cost factor is the first defence and the 600/min IP guard the
    second; neither is sized for this, which is why the POST carries a third.

    Run at the shipped default rather than a test-only limit, because the limit
    is fixed when the routes are assembled and reassembling them would mean a
    second application. Fifteen attempts against a bucket of ten is a clear
    enough signal, and exercising the real number is the better test anyway.
    """

    client_id = register(oauth_client)
    statuses = []
    for _ in range(15):
        params = authorize(oauth_client, client_id)
        statuses.append(
            submit_login(oauth_client, params, password="wrong").status_code
        )

    # The GET is deliberately unlimited: rendering a form is cheap, and an
    # operator reloading one is ordinary.
    params = authorize(oauth_client, client_id)
    form = oauth_client.get(
        LOGIN_PATH, params={"req": params["req"][0], "csrf": params["csrf"][0]}
    )

    assert statuses[0] == 400
    assert 429 in statuses
    assert form.status_code == 200


# ------------------------------------------------------------- helpers ----


def _minted_scopes() -> list[list[str]]:
    """Scopes on every credential this module's flows created."""

    transactions, engine = vault_service()

    async def run() -> list[list[str]]:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(vault_agent_credentials.c.scopes).where(
                        vault_agent_credentials.c.principal_id.like(
                            f"{PRINCIPAL_PREFIX}%"
                        )
                    )
                )
                return [sorted(row[0]) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_the_principal_reads_as_a_name_not_a_uuid() -> None:
    """It reaches ``contributed_by`` on every note the client writes."""

    assert PRINCIPAL_PREFIX == "oauth-"
    assert os.sep not in PRINCIPAL_PREFIX


# ------------------------------------------- revocation and replay races ----


def test_revoking_an_access_token_kills_the_grant(oauth_client: TestClient) -> None:
    """The SDK only calls `revoke_token` when the loaded token's `client_id`
    matches the authenticated client's.

    `load_access_token` used to return `principal_id` -- `oauth-<slug>` -- which
    never equals a registration uuid, so `/revoke` answered 200 and revoked
    nothing at all. The refresh-token path masked it, because a refresh token
    already carried the real client id.

    And revoking the credential alone is not revocation either: its refresh
    token would mint a replacement seconds later. Both halves are asserted here.
    """

    registration = register_full(oauth_client)
    client_id = registration["client_id"]
    params = authorize(oauth_client, client_id)
    redirect = submit_login(oauth_client, params)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
    tokens = exchange(oauth_client, client_id, code)

    revoked = oauth_client.post(
        "/revoke",
        data={
            "token": tokens["access_token"],
            "token_type_hint": "access_token",
            "client_id": client_id,
            "client_secret": registration.get("client_secret") or "",
        },
    )

    assert revoked.status_code == 200
    used = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert used.status_code == 401
    # The refresh token cannot resurrect the grant.
    renewed = _refresh(oauth_client, client_id, tokens["refresh_token"])
    assert renewed.status_code == 400


def test_an_operator_credential_is_not_revocable_through_oauth(
    oauth_client: TestClient,
) -> None:
    """`/revoke` is callable by any self-registered client.

    An `hssv1_` credential minted by `issue_vault_credential` belongs to no
    OAuth client, so `load_access_token` returns None and the endpoint cannot
    reach it. Without that, a registered client could name an operator
    credential and have it revoked.
    """

    registration = register_full(oauth_client)
    credential_id, operator_token = _issue((VaultScope.READ,))
    try:
        revoked = oauth_client.post(
            "/revoke",
            data={
                "token": operator_token,
                "token_type_hint": "access_token",
                "client_id": registration["client_id"],
                "client_secret": registration.get("client_secret") or "",
            },
        )
        assert revoked.status_code == 200  # RFC 7009: 200 whatever happened
        still_works = oauth_client.get(
            "/api/v1/vault/search",
            params={"q": "x"},
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert still_works.status_code == 200
    finally:
        _drop(credential_id)


def test_a_concurrent_refresh_loser_burns_the_family(
    oauth_client: TestClient,
) -> None:
    """The race rotation-with-detection exists to catch.

    `load_refresh_token` checks `consumed_at`, but the SDK loads and exchanges
    in separate calls, so two requests can both pass that check while the row is
    unconsumed. One wins the conditional consume; the other used to get a plain
    `invalid_grant` and leave the family alive -- which is the stolen-token case
    going undetected.

    Driven sequentially rather than with real threads: the loser's distinguishing
    condition is "the row is now consumed", which is exactly the state a second
    exchange of the same token observes. That is what the fix keys on.
    """

    client_id, tokens = full_flow(oauth_client)
    first = _refresh(oauth_client, client_id, tokens["refresh_token"])
    assert first.status_code == 200
    renewed = first.json()

    # The loser of the race presents the same token again.
    loser = _refresh(oauth_client, client_id, tokens["refresh_token"])

    assert loser.status_code == 400
    # The winner's replacement is dead too -- that is what "family" means.
    assert _refresh(oauth_client, client_id, renewed["refresh_token"]).status_code == 400
    live = oauth_client.get(
        "/api/v1/vault/search",
        params={"q": "x"},
        headers={"Authorization": f"Bearer {renewed['access_token']}"},
    )
    assert live.status_code == 401


def test_the_public_oauth_routes_carry_the_preauth_guard() -> None:
    """ADR 0024 requires it and it was not implemented.

    These are root-mounted Starlette routes, so they inherit neither the vault
    router's dependency nor the MCP mount's middleware -- `/register` was an
    unauthenticated endpoint that writes a row, with no limit at all.

    Asserted structurally rather than by exhausting a bucket: the limit is
    600/minute and spending it in a test would be slower than the rest of the
    suite combined, and would leave the shared IP bucket empty for whatever ran
    next.
    """

    from app.main import create_app

    routes = create_app().router.routes
    guarded = {
        route.path
        for route in routes
        if getattr(route, "path", "") in ("/register", "/authorize", "/token", "/revoke")
        and getattr(getattr(route, "app", None), "__name__", "") == "guarded"
    }

    assert guarded == {"/register", "/authorize", "/token", "/revoke"}

    # The login POST is deliberately *not* wrapped: it carries its own 10/minute
    # bucket, and wrapping it suppressed that entirely -- trading the tighter
    # limit for the looser one. One bucket per endpoint, the tightest that
    # applies.
    login_post = [
        route
        for route in routes
        if getattr(route, "path", "") == LOGIN_PATH and "POST" in (route.methods or ())
    ]
    assert len(login_post) == 1
    assert getattr(login_post[0].app, "__name__", "") != "guarded"
    assert hasattr(login_post[0].endpoint, "__wrapped__")


# --------------------------------------------- who the operator approves ----


def _authorize_to(client: TestClient, client_id: str, redirect_uri: str):
    """``authorize`` for a client whose redirect is not the shared one."""

    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": " ".join(OAUTH_BASELINE_SCOPES),
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)


def _consent_page(client: TestClient, params: dict) -> str:
    page = client.get(
        LOGIN_PATH, params={"req": params["req"][0], "csrf": params["csrf"][0]}
    )
    assert page.status_code == 200, page.text
    return page.text


def _register_to(client: TestClient, *, name: str, redirect_uri: str) -> str:
    response = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["client_id"]


def test_two_clients_sharing_a_name_are_distinguishable_at_consent(
    oauth_client: TestClient,
) -> None:
    """The impersonation the consent screen exists to stop.

    Registration is open, so `client_name` is attacker-controlled: anyone may
    register as "Claude", point the redirect at a host they control, and mail
    the operator a real `/authorize` link on the real vault domain. The page
    used to lead with that name and show nothing else about the client, so the
    two requests rendered identically -- and approving the wrong one hands
    `vault:read` and `vault:write` to the attacker.

    What separates them is where the code is delivered, and which registration
    it is. Both are on the page now, and this asserts they actually differ
    between two clients that named themselves the same thing.
    """

    honest = _register_to(oauth_client, name="Claude", redirect_uri=REDIRECT_URI)
    impostor_redirect = "https://claude-ai.example.net/callback"
    impostor = _register_to(
        oauth_client, name="Claude", redirect_uri=impostor_redirect
    )

    honest_page = _consent_page(oauth_client, authorize(oauth_client, honest))
    impostor_page = _consent_page(
        oauth_client, _authorize_to(oauth_client, impostor, impostor_redirect)
    )

    # The name alone tells the operator nothing -- both pages carry it.
    assert "Claude" in honest_page and "Claude" in impostor_page

    assert "https://claude.ai" in honest_page
    assert "https://claude-ai.example.net" not in honest_page
    assert "https://claude-ai.example.net" in impostor_page
    assert "https://claude.ai" not in impostor_page

    assert honest in honest_page and honest not in impostor_page
    assert impostor in impostor_page and impostor not in honest_page


def test_the_consent_screen_calls_the_name_unverified(
    oauth_client: TestClient,
) -> None:
    """Shown as a claim, not as identity.

    An operator who reads the name as the vault vouching for it has been
    misled by the page rather than informed by it.
    """

    client_id = register(oauth_client, name="Claude")
    page = _consent_page(oauth_client, authorize(oauth_client, client_id))

    assert "calling itself" in page
    assert "not verified" in page


def test_an_overlong_client_name_cannot_bury_the_destination(
    oauth_client: TestClient,
) -> None:
    """The name is unbounded attacker input, and it renders above the rest.

    A few thousand characters of it -- or a few thousand newlines -- would push
    the destination, the scopes and the caution off the screen, leaving a
    password field under something that looks like a complete page.
    """

    client_id = _register_to(
        oauth_client,
        name="Claude " + "padding " * 500,
        redirect_uri=REDIRECT_URI,
    )
    page = _consent_page(oauth_client, authorize(oauth_client, client_id))

    assert page.count("padding") < 20
    assert "https://claude.ai" in page
    assert "not verified" in page


# ------------------------------------------------- principals and pruning ----


def _principal_of(token: str) -> str:
    """The principal recorded on the credential a token names."""

    credential_id = token.split("_")[1]
    transactions, engine = vault_service()

    async def read() -> str:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(vault_agent_credentials.c.principal_id).where(
                        vault_agent_credentials.c.id == credential_id
                    )
                )
                return result.scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_two_clients_with_one_name_get_different_principals(
    oauth_client: TestClient,
) -> None:
    """`principal_id` is the actor across three isolation boundaries.

    It was `oauth-<slug(client_name)>`, and registration is open, so two
    separately approved clients calling themselves "Claude" became one actor:
    one idempotency namespace (`vault_write_requests` is keyed on
    `(principal_id, idempotency_key)`), one quota bucket, and one indistinct
    audit trail. The old comment called that deliberate -- "the same logical
    client" -- which only holds if the name means something, and nothing checks
    that it does.
    """

    _, first = full_flow(oauth_client, name="Claude")
    _, second = full_flow(oauth_client, name="Claude")

    assert _principal_of(first["access_token"]) != _principal_of(
        second["access_token"]
    )


def test_names_that_slugify_alike_get_different_principals(
    oauth_client: TestClient,
) -> None:
    """The collision did not need two identical names, only two that normalize
    alike -- case, punctuation, or anything past the slug's length limit."""

    _, plain = full_flow(oauth_client, name="Claude")
    _, shouted = full_flow(oauth_client, name="CLAUDE!")

    assert _principal_of(plain["access_token"]) != _principal_of(
        shouted["access_token"]
    )


def test_a_principal_is_the_registration_id(oauth_client: TestClient) -> None:
    """Not the name, and not a slug of it.

    Pinned explicitly because the readable-`contributed_by` argument that
    produced the old scheme is a real cost and someone will be tempted back.
    `display_name` is where the client's name belongs.
    """

    client_id, tokens = full_flow(oauth_client, name="Claude")

    assert _principal_of(tokens["access_token"]) == f"{PRINCIPAL_PREFIX}{client_id}"
    assert "claude" not in _principal_of(tokens["access_token"])


async def _delete_client(client_id: str) -> None:
    transactions, engine = vault_service()
    try:
        async with transactions.transaction() as connection:
            await connection.execute(
                delete(vault_oauth_clients).where(
                    vault_oauth_clients.c.client_id == client_id
                )
            )
    finally:
        await engine.dispose()


def test_a_registration_pruned_mid_authorization_is_a_protocol_error(
    oauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prune/authorize race, reproduced at the point it actually opens.

    Deleting the client *before* the request proves nothing: the SDK looks the
    client up first and refuses cleanly on its own. The window is narrower than
    that -- the lookup succeeds, and the sweep commits before `authorize` writes
    the pending row in its own separate transaction. So the deletion is hung off
    `get_client`, which puts it exactly there.

    Unfixed this is a foreign-key violation on the insert: a 500 on a flow that
    was valid when it started. Fixed, `authorize` re-reads the registration under
    the shared lock and answers `unauthorized_client`, which tells the client to
    register again -- the one useful thing to say.
    """

    from app.vault.oauth import VaultAuthorizationProvider

    client_id = register(oauth_client, name="Claude")
    original = VaultAuthorizationProvider.get_client

    async def get_then_prune(self, looked_up_id: str):
        client = await original(self, looked_up_id)
        if client is not None and looked_up_id == client_id:
            # The sweep lands here: after the lookup, before `authorize`.
            await _delete_client(client_id)
        return client

    monkeypatch.setattr(VaultAuthorizationProvider, "get_client", get_then_prune)

    response = oauth_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": " ".join(OAUTH_BASELINE_SCOPES),
        },
        follow_redirects=False,
    )

    # The protocol's own vocabulary -- an error redirect back to the client,
    # never a stack trace. Asserted unconditionally rather than behind an
    # `if redirect` branch, which would stop testing anything the day the
    # status changed.
    assert response.status_code == 302, response.text
    assert "unauthorized_client" in response.headers["location"]


def test_pruning_and_authorizing_take_the_same_lock() -> None:
    """Both halves, asserted on the source.

    The property is that two code paths name one lock, and a timing test for it
    would be slow, flaky, and would still not prove the pairing -- while one of
    them silently dropping the lock is precisely the regression that reopens the
    race.
    """

    import inspect

    from app.vault.constants import OAUTH_CLIENT_LOCK_KEY
    from app.vault.oauth import VaultAuthorizationProvider
    from app.vault.repository import VaultOAuthClientRepository

    for method in (
        VaultAuthorizationProvider.authorize,
        VaultOAuthClientRepository.delete_stale,
    ):
        source = inspect.getsource(method)
        assert "pg_advisory_xact_lock" in source, method.__qualname__
        assert "OAUTH_CLIENT_LOCK_KEY" in source, method.__qualname__

    assert OAUTH_CLIENT_LOCK_KEY != 0x5641554C5401  # not the corpus lock
