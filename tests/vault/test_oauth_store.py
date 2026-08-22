"""The OAuth authorization server's persistence, per ADR 0024.

Against the database rather than a stand-in, because the properties worth
pinning are all facts about Postgres: that single use is one atomic statement
and not two, that expiry is evaluated by the server rather than by the caller's
clock, and that a pruned client takes its in-flight authorizations with it.

Nothing here issues a token. An access token is a ``vault_agent_credentials``
row, which is the whole of ADR 0024 and the reason there is no fourth table.
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.vault.constants import (
    AUTHORIZATION_CODE_TTL_SECONDS,
    OAUTH_BASELINE_SCOPES,
    PENDING_AUTHORIZATION_TTL_SECONDS,
)
from app.vault.repository import (
    VaultOAuthAuthorizationCodeRepository,
    VaultOAuthClientRepository,
    VaultOAuthPendingAuthorizationRepository,
    hash_oauth_secret,
)
from app.vault.tables import (
    vault_oauth_authorization_codes,
    vault_oauth_clients,
    vault_oauth_pending_authorizations,
)
from tests.vault.test_search import vault_service


CLIENT_PREFIX = "test-oauth-"


def run(coroutine_factory):
    """Run one coroutine against a fresh engine, disposing it afterwards."""

    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await coroutine_factory(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _cleanup() -> None:
    async def remove(connection) -> None:
        # Codes and pending rows cascade from the client, but a test may have
        # created one for a client another test already removed.
        await connection.execute(
            delete(vault_oauth_authorization_codes).where(
                vault_oauth_authorization_codes.c.client_id.like(f"{CLIENT_PREFIX}%")
            )
        )
        await connection.execute(
            delete(vault_oauth_pending_authorizations).where(
                vault_oauth_pending_authorizations.c.client_id.like(
                    f"{CLIENT_PREFIX}%"
                )
            )
        )
        await connection.execute(
            delete(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id.like(f"{CLIENT_PREFIX}%")
            )
        )

    run(remove)


@pytest.fixture(autouse=True)
def clean_oauth_tables(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _client_info(client_id: str) -> dict[str, Any]:
    """A registration shaped like the SDK's OAuthClientInformationFull.

    Kept as a plain dict rather than built from the SDK model, because the
    point of storing JSONB is that persistence does not know the model. A test
    that constructed one would be asserting the SDK's shape rather than this
    table's contract.
    """

    return {
        "client_id": client_id,
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "client_name": "Claude",
        "scope": " ".join(OAUTH_BASELINE_SCOPES),
        "token_endpoint_auth_method": "client_secret_post",
        # A key neither this schema nor the current SDK model has a field for.
        # It has to survive the round trip, which is why the column is JSONB.
        "x_unmodelled_extension": {"nested": ["value"]},
    }


def _register(client_id: str | None = None, **kwargs: Any) -> str:
    client_id = client_id or f"{CLIENT_PREFIX}{uuid4().hex}"

    async def create(connection):
        return await VaultOAuthClientRepository().upsert(
            connection,
            client_id=client_id,
            client_info=_client_info(client_id),
            **kwargs,
        )

    run(create)
    return client_id


# ------------------------------------------------------------- clients ----


def test_a_registration_round_trips_including_unmodelled_keys() -> None:
    client_id = _register()

    stored = run(lambda c: VaultOAuthClientRepository().get(c, client_id))

    assert stored is not None
    assert stored.client_id == client_id
    assert stored.client_info == _client_info(client_id)
    assert stored.expires_at is None


def test_an_unknown_client_is_none_rather_than_an_error() -> None:
    """``get_client`` returning None is how the SDK renders an OAuth error.

    Raising here would surface as a 500 instead of the ``invalid_client`` the
    specification calls for.
    """

    missing = run(
        lambda c: VaultOAuthClientRepository().get(c, f"{CLIENT_PREFIX}{uuid4().hex}")
    )

    assert missing is None


def test_re_registering_replaces_rather_than_conflicting() -> None:
    """A client that lost its secret repeats the flow under an id it still holds.

    Refusing would leave it permanently unable to reconnect, and a registration
    carries no history worth preserving over the current one.
    """

    client_id = _register()

    async def re_register(connection):
        return await VaultOAuthClientRepository().upsert(
            connection,
            client_id=client_id,
            client_info={"client_id": client_id, "client_name": "Renamed"},
        )

    updated = run(re_register)

    assert updated.client_info["client_name"] == "Renamed"
    assert "x_unmodelled_extension" not in updated.client_info


def test_pruning_removes_only_expired_registrations() -> None:
    expired = _register()
    permanent = _register()
    run(
        lambda c: c.execute(
            vault_oauth_clients.update()
            .where(vault_oauth_clients.c.client_id == expired)
            .values(
                registered_at=text("now() - interval '1 hour'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    )

    removed = run(lambda c: VaultOAuthClientRepository().delete_expired(c))

    assert removed == 1
    assert run(lambda c: VaultOAuthClientRepository().get(c, expired)) is None
    assert run(lambda c: VaultOAuthClientRepository().get(c, permanent)) is not None


# --------------------------------------------- pending authorizations ----


def _pending_params() -> dict[str, Any]:
    return {
        "state": "opaque-client-state",
        "scopes": list(OAUTH_BASELINE_SCOPES),
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }


def test_a_pending_authorization_survives_to_be_redeemed_once() -> None:
    """The whole reason this is a table: the two halves cross workers.

    Registration is server-to-server from the vendor's backend; ``/authorize``
    is a browser navigation. An in-memory dict fails deterministically here,
    and only in production.
    """

    client_id = _register()
    nonce = uuid4().hex

    async def create(connection):
        return await VaultOAuthPendingAuthorizationRepository().create(
            connection,
            nonce=nonce,
            client_id=client_id,
            params=_pending_params(),
        )

    created = run(create)
    assert created.params == _pending_params()

    redeemed = run(
        lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, nonce)
    )
    replayed = run(
        lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, nonce)
    )

    assert redeemed is not None
    assert redeemed.client_id == client_id
    assert redeemed.params["code_challenge"] == _pending_params()["code_challenge"]
    assert replayed is None


def test_an_unknown_nonce_and_an_expired_one_are_indistinguishable() -> None:
    """ADR 0024: one failure message, whatever failed.

    A login page that told "expired" from "never existed" apart would hand an
    attacker a probe for valid authorization attempts. The repository makes
    that easy to honour by returning None for both.
    """

    client_id = _register()
    nonce = uuid4().hex
    run(
        lambda c: VaultOAuthPendingAuthorizationRepository().create(
            c, nonce=nonce, client_id=client_id, params=_pending_params()
        )
    )
    run(
        lambda c: c.execute(
            vault_oauth_pending_authorizations.update()
            .where(
                vault_oauth_pending_authorizations.c.nonce_sha256
                == hash_oauth_secret(nonce)
            )
            .values(
                created_at=text("now() - interval '1 hour'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    )

    expired = run(
        lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, nonce)
    )
    never_existed = run(
        lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, uuid4().hex)
    )

    assert expired is None
    assert never_existed is None


def test_the_nonce_is_stored_hashed_and_never_in_the_clear() -> None:
    """ADR 0015's rule: only a digest of a machine-generated secret is kept."""

    client_id = _register()
    nonce = uuid4().hex
    run(
        lambda c: VaultOAuthPendingAuthorizationRepository().create(
            c, nonce=nonce, client_id=client_id, params=_pending_params()
        )
    )

    stored = run(
        lambda c: c.execute(
            select(vault_oauth_pending_authorizations.c.nonce_sha256).where(
                vault_oauth_pending_authorizations.c.client_id == client_id
            )
        )
    )
    digests = [row[0] for row in stored]

    assert digests == [hash_oauth_secret(nonce)]
    assert nonce.encode("utf-8") not in digests[0]


def test_dropping_a_client_takes_its_pending_authorizations_with_it() -> None:
    """An authorization in flight for a pruned client cannot complete."""

    client_id = _register()
    nonce = uuid4().hex
    run(
        lambda c: VaultOAuthPendingAuthorizationRepository().create(
            c, nonce=nonce, client_id=client_id, params=_pending_params()
        )
    )

    run(
        lambda c: c.execute(
            delete(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )
    )

    assert (
        run(lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, nonce))
        is None
    )


def test_pruning_clears_expired_pending_authorizations() -> None:
    client_id = _register()
    stale, live = uuid4().hex, uuid4().hex
    for nonce in (stale, live):
        run(
            lambda c, n=nonce: VaultOAuthPendingAuthorizationRepository().create(
                c, nonce=n, client_id=client_id, params=_pending_params()
            )
        )
    run(
        lambda c: c.execute(
            vault_oauth_pending_authorizations.update()
            .where(
                vault_oauth_pending_authorizations.c.nonce_sha256
                == hash_oauth_secret(stale)
            )
            .values(
                created_at=text("now() - interval '1 hour'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    )

    removed = run(
        lambda c: VaultOAuthPendingAuthorizationRepository().delete_expired(c)
    )

    assert removed == 1
    assert (
        run(lambda c: VaultOAuthPendingAuthorizationRepository().redeem(c, live))
        is not None
    )


# --------------------------------------------------- authorization codes ----


def _mint(client_id: str, code: str, **kwargs: Any):
    async def create(connection):
        return await VaultOAuthAuthorizationCodeRepository().create(
            connection,
            code=code,
            client_id=client_id,
            scopes=OAUTH_BASELINE_SCOPES,
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            redirect_uri_provided_explicitly=True,
            **kwargs,
        )

    return run(create)


def test_a_code_round_trips_every_field_the_exchange_needs() -> None:
    client_id = _register()
    code = uuid4().hex

    minted = _mint(client_id, code, subject="operator:password")
    loaded = run(lambda c: VaultOAuthAuthorizationCodeRepository().get(c, code))

    assert minted.scopes == OAUTH_BASELINE_SCOPES
    assert loaded is not None
    assert loaded.client_id == client_id
    assert loaded.code_challenge == minted.code_challenge
    assert loaded.redirect_uri == "https://claude.ai/api/mcp/auth_callback"
    assert loaded.redirect_uri_provided_explicitly is True
    assert loaded.subject == "operator:password"


def test_loading_a_code_does_not_consume_it() -> None:
    """The SDK splits ``load_authorization_code`` from the exchange.

    A consuming load would destroy a code on a failed exchange, when the client
    is still entitled to use it.
    """

    client_id = _register()
    code = uuid4().hex
    _mint(client_id, code)

    run(lambda c: VaultOAuthAuthorizationCodeRepository().get(c, code))
    run(lambda c: VaultOAuthAuthorizationCodeRepository().get(c, code))

    assert run(lambda c: VaultOAuthAuthorizationCodeRepository().redeem(c, code))


def test_a_code_redeems_exactly_once() -> None:
    """RFC 6749 requires single use, and this is what makes reuse detectable."""

    client_id = _register()
    code = uuid4().hex
    _mint(client_id, code)

    first = run(lambda c: VaultOAuthAuthorizationCodeRepository().redeem(c, code))
    second = run(lambda c: VaultOAuthAuthorizationCodeRepository().redeem(c, code))

    assert first is not None
    assert second is None


def test_an_expired_code_neither_loads_nor_redeems() -> None:
    """Expiry is in the SQL predicate, so the database clock decides it."""

    client_id = _register()
    code = uuid4().hex
    _mint(client_id, code)
    run(
        lambda c: c.execute(
            vault_oauth_authorization_codes.update()
            .where(
                vault_oauth_authorization_codes.c.code_sha256
                == hash_oauth_secret(code)
            )
            .values(
                created_at=text("now() - interval '1 hour'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    )

    assert run(lambda c: VaultOAuthAuthorizationCodeRepository().get(c, code)) is None
    assert (
        run(lambda c: VaultOAuthAuthorizationCodeRepository().redeem(c, code)) is None
    )


def test_the_code_is_stored_hashed() -> None:
    client_id = _register()
    code = uuid4().hex
    _mint(client_id, code)

    rows = run(
        lambda c: c.execute(
            select(vault_oauth_authorization_codes.c.code_sha256).where(
                vault_oauth_authorization_codes.c.client_id == client_id
            )
        )
    )

    assert [row[0] for row in rows] == [hash_oauth_secret(code)]


def test_a_code_may_carry_scopes_above_the_oauth_baseline() -> None:
    """ADR 0024 makes the baseline what a client may *request*, not a ceiling.

    An operator widens a specific credential afterwards, which the ADR calls
    expected rather than exceptional -- so the CHECK here mirrors
    ``vault_agent_credentials_scopes_known`` and not the narrower baseline. A
    stricter column constraint would forbid the widened case at the database
    layer, where no application code could permit it.
    """

    client_id = _register()
    code = uuid4().hex

    async def create(connection):
        return await VaultOAuthAuthorizationCodeRepository().create(
            connection,
            code=code,
            client_id=client_id,
            scopes=("vault:read", "vault:write", "vault:update"),
            code_challenge="challenge",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            redirect_uri_provided_explicitly=True,
        )

    minted = run(create)

    assert minted.scopes == ("vault:read", "vault:write", "vault:update")


def test_an_unknown_scope_is_refused_by_the_database() -> None:
    """The last line, below whatever application code believes."""

    client_id = _register()

    async def create(connection):
        return await VaultOAuthAuthorizationCodeRepository().create(
            connection,
            code=uuid4().hex,
            client_id=client_id,
            scopes=("vault:read", "vault:invent"),
            code_challenge="challenge",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            redirect_uri_provided_explicitly=True,
        )

    with pytest.raises(Exception, match="vault_oauth_codes_scopes_known"):
        run(create)


def test_the_ttls_are_ordered_as_the_flow_needs() -> None:
    """A code outliving the wait for a person would be the wrong way round.

    A pending authorization waits on someone reading a consent screen; a code
    is redeemed by a machine immediately. Not a database fact, but the one
    relationship between the two constants that has to hold.
    """

    assert AUTHORIZATION_CODE_TTL_SECONDS < PENDING_AUTHORIZATION_TTL_SECONDS
