"""Pruning the OAuth authorization server's transient state.

The assertions worth having are all about what is *spared*. Removing expired
rows is easy to get right; the ways this goes wrong are deleting a live
credential, deleting an operator-issued one, or deleting a consumed refresh
token that replay detection still needs.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text

from app.vault.auth import VaultScope, hash_secret
from app.vault.oauth import PRINCIPAL_PREFIX
from app.vault.repository import VaultOAuthClientRepository
from app.vault.tables import (
    vault_agent_credentials,
    vault_oauth_authorization_codes,
    vault_oauth_clients,
    vault_oauth_pending_authorizations,
    vault_oauth_refresh_tokens,
)
from scripts.prune_vault_oauth import prune
from tests.vault.test_search import vault_service


OPERATOR_PRINCIPAL = "test-prune-operator"
OAUTH_PRINCIPAL = f"{PRINCIPAL_PREFIX}test-prune"


def _run(factory):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await factory(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _credential(
    principal_id: str,
    *,
    revoked_days_ago: int | None,
) -> str:
    credential_id = uuid4().hex[:16]

    async def create(connection):
        await connection.execute(
            insert(vault_agent_credentials).values(
                id=credential_id,
                principal_id=principal_id,
                display_name="prune fixture",
                secret_sha256=hash_secret(uuid4().hex),
                scopes=[VaultScope.READ],
                revoked_at=(
                    None
                    if revoked_days_ago is None
                    else datetime.now(UTC) - timedelta(days=revoked_days_ago)
                ),
            )
        )

    _run(create)
    return credential_id


def _exists(credential_id: str) -> bool:
    def read(connection):
        return connection.execute(
            select(vault_agent_credentials.c.id).where(
                vault_agent_credentials.c.id == credential_id
            )
        )

    return _run(read).one_or_none() is not None


def _cleanup() -> None:
    async def remove(connection):
        await connection.execute(
            delete(vault_oauth_refresh_tokens).where(
                vault_oauth_refresh_tokens.c.client_id.like("test-prune-%")
            )
        )
        await connection.execute(
            delete(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id.like("test-prune-%")
            )
        )
        await connection.execute(
            delete(vault_agent_credentials).where(
                vault_agent_credentials.c.principal_id.in_(
                    [OPERATOR_PRINCIPAL, OAUTH_PRINCIPAL]
                )
            )
        )

    _run(remove)


@pytest.fixture(autouse=True)
def clean(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _prune(dry_run: bool = False, retention_days: int = 30) -> None:
    asyncio.run(prune(dry_run, retention_days))


def test_a_long_revoked_oauth_credential_is_removed(capsys) -> None:
    stale = _credential(OAUTH_PRINCIPAL, revoked_days_ago=45)

    _prune()

    assert not _exists(stale)


def test_a_recently_revoked_oauth_credential_is_kept(capsys) -> None:
    """The window is for reading a recent incident."""

    recent = _credential(OAUTH_PRINCIPAL, revoked_days_ago=3)

    _prune()

    assert _exists(recent)


def test_a_live_oauth_credential_is_never_a_candidate(capsys) -> None:
    """The failure a count-based rule would have: two registrations sharing a
    principal, and the older one's live credential deleted as "not the newest".

    Age with a ``revoked_at IS NOT NULL`` predicate cannot express that mistake:
    an active row is out of scope whatever its age or how many newer siblings
    share its principal.
    """

    live = _credential(OAUTH_PRINCIPAL, revoked_days_ago=None)
    _credential(OAUTH_PRINCIPAL, revoked_days_ago=45)
    _credential(OAUTH_PRINCIPAL, revoked_days_ago=45)

    _prune()

    assert _exists(live)


def test_an_operator_issued_credential_is_never_pruned(capsys) -> None:
    """Even revoked, even ancient.

    Those rows are a census an operator reads -- `issue_vault_credential list`
    is how you see that `importer` was revoked and when. Machine turnover is
    what this script is for.
    """

    operator = _credential(OPERATOR_PRINCIPAL, revoked_days_ago=400)

    _prune()

    assert _exists(operator)


def test_a_dry_run_removes_nothing(capsys) -> None:
    stale = _credential(OAUTH_PRINCIPAL, revoked_days_ago=45)

    _prune(dry_run=True)
    output = capsys.readouterr().out

    assert _exists(stale)
    assert "would remove" in output


def test_a_consumed_refresh_token_survives_until_it_expires(capsys) -> None:
    """Replay detection needs the consumed digest, which is the whole point.

    Pruning on consumption would delete exactly the evidence that lets a
    replayed token be recognised and its family revoked. The predicate is
    expiry, and this pins it.
    """

    client_id = f"test-prune-{uuid4().hex}"
    credential_id = _credential(OAUTH_PRINCIPAL, revoked_days_ago=None)
    token_digest = uuid4().bytes + uuid4().bytes

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id, client_info={"client_id": client_id}
            )
        )
        await connection.execute(
            insert(vault_oauth_refresh_tokens).values(
                token_sha256=token_digest,
                family_id=uuid4(),
                client_id=client_id,
                credential_id=credential_id,
                scopes=[VaultScope.READ],
                expires_at=text("now() + interval '10 days'"),
                consumed_at=text("now()"),
            )
        )

    _run(seed)

    _prune()

    def read(connection):
        return connection.execute(
            select(vault_oauth_refresh_tokens.c.token_sha256).where(
                vault_oauth_refresh_tokens.c.token_sha256 == token_digest
            )
        )

    assert _run(read).one_or_none() is not None


def test_an_idle_registration_becomes_prunable(capsys) -> None:
    """Registrations used to be immortal.

    `register_client` supplies no expiry, the SDK leaves
    `client_secret_expiry_seconds` unset, and the old sweep filtered on
    `expires_at IS NOT NULL` -- so it deleted nothing while `/register`, a
    public endpoint, grew the table on every call.
    """

    client_id = f"test-prune-{uuid4().hex}"

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id,
                client_info={"client_id": client_id},
                registered_at=text("now() - interval '90 days'"),
            )
        )

    _run(seed)

    _prune()

    def read(connection):
        return connection.execute(
            select(vault_oauth_clients.c.client_id).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )

    assert _run(read).one_or_none() is None


def test_a_registration_with_a_live_refresh_token_is_never_pruned(capsys) -> None:
    """Deleting one cascades to its tokens and revokes a working connector.

    Age alone is not enough: a client registered long ago and still renewing is
    exactly the client that must keep working. Liveness is an unconsumed,
    unexpired refresh token.
    """

    client_id = f"test-prune-{uuid4().hex}"
    credential_id = _credential(OAUTH_PRINCIPAL, revoked_days_ago=None)

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id,
                client_info={"client_id": client_id},
                registered_at=text("now() - interval '90 days'"),
            )
        )
        await connection.execute(
            insert(vault_oauth_refresh_tokens).values(
                token_sha256=uuid4().bytes + uuid4().bytes,
                family_id=uuid4(),
                client_id=client_id,
                credential_id=credential_id,
                scopes=[VaultScope.READ],
                expires_at=text("now() + interval '10 days'"),
            )
        )

    _run(seed)

    _prune()

    def read(connection):
        return connection.execute(
            select(vault_oauth_clients.c.client_id).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )

    assert _run(read).one_or_none() is not None


def test_a_recent_registration_is_kept_even_with_no_tokens(capsys) -> None:
    """An authorization in progress has a registration and not yet a token."""

    client_id = f"test-prune-{uuid4().hex}"

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id, client_info={"client_id": client_id}
            )
        )

    _run(seed)

    _prune()

    def read(connection):
        return connection.execute(
            select(vault_oauth_clients.c.client_id).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )

    assert _run(read).one_or_none() is not None


def _old_client(client_id: str) -> None:
    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id,
                client_info={"client_id": client_id},
                registered_at=text("now() - interval '90 days'"),
            )
        )

    _run(seed)


def _client_survives(client_id: str) -> bool:
    def read(connection):
        return connection.execute(
            select(vault_oauth_clients.c.client_id).where(
                vault_oauth_clients.c.client_id == client_id
            )
        )

    return _run(read).one_or_none() is not None


def _park_pending(client_id: str, *, expires_in: str) -> None:
    """An authorization parked at the consent screen.

    ``created_at`` is dated back an hour so an already-expired row can be
    written at all: the table checks ``expires_at > created_at``, which the
    default ``now()`` would violate.
    """

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_pending_authorizations).values(
                nonce_sha256=uuid4().bytes + uuid4().bytes,
                client_id=client_id,
                params={"redirect_uri": "https://example.test/cb"},
                created_at=text("now() - interval '1 hour'"),
                expires_at=text(f"now() + interval '{expires_in}'"),
            )
        )

    _run(seed)


def _park_code(client_id: str, *, expires_in: str) -> None:
    """A code minted and not yet exchanged. Same dating trick as above."""

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_authorization_codes).values(
                code_sha256=uuid4().bytes + uuid4().bytes,
                client_id=client_id,
                scopes=[VaultScope.READ],
                code_challenge="a-pkce-challenge",
                redirect_uri="https://example.test/cb",
                redirect_uri_provided_explicitly=True,
                created_at=text("now() - interval '1 hour'"),
                expires_at=text(f"now() + interval '{expires_in}'"),
            )
        )

    _run(seed)


def test_a_registration_on_the_consent_screen_is_never_pruned(capsys) -> None:
    """Age plus a live refresh token was not enough liveness.

    A pending authorization cascades on delete, so pruning while the operator
    is looking at the consent page destroys the flow: the login POST finds no
    row and renders the same failure as a bad password, with nothing anywhere
    saying a sweep took it.

    Not an exotic window either. An old registration whose refresh token has
    expired is exactly the client that reconnects, and reconnecting means
    starting here.
    """

    client_id = f"test-prune-{uuid4().hex}"
    _old_client(client_id)
    _park_pending(client_id, expires_in="5 minutes")

    _prune()

    assert _client_survives(client_id)


def test_a_registration_mid_code_exchange_is_never_pruned(capsys) -> None:
    """The other half of the window: code minted, not yet exchanged."""

    client_id = f"test-prune-{uuid4().hex}"
    _old_client(client_id)
    _park_code(client_id, expires_in="60 seconds")

    _prune()

    assert _client_survives(client_id)


def test_expired_in_flight_rows_do_not_make_a_registration_immortal(
    capsys,
) -> None:
    """Sparing anything referenced at all would be the opposite mistake.

    An abandoned authorization is a row that stays forever, so it would pin its
    client forever too, and the sweep would go back to deleting nothing. The
    predicate is unexpired, and this is what pins that.
    """

    client_id = f"test-prune-{uuid4().hex}"
    _old_client(client_id)
    _park_pending(client_id, expires_in="-5 minutes")
    _park_code(client_id, expires_in="-60 seconds")

    _prune()

    assert not _client_survives(client_id)


def test_the_dry_run_count_matches_what_the_delete_removes(capsys) -> None:
    """A preview that disagrees with the delete is worse than no preview.

    Both used to build the predicate separately, in two files, which is the
    drift this asserts against: the count is taken, then the same state is
    actually pruned, and the two have to agree.
    """

    doomed = f"test-prune-{uuid4().hex}"
    spared = f"test-prune-{uuid4().hex}"
    _old_client(doomed)
    _old_client(spared)
    _park_pending(spared, expires_in="5 minutes")

    async def counted(connection):
        return await VaultOAuthClientRepository().count_stale(connection, 30)

    before = _run(counted)

    _prune()

    assert before == 1
    assert not _client_survives(doomed)
    assert _client_survives(spared)
