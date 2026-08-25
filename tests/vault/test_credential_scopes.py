"""Widening and narrowing a static credential without rotating its secret.

OAuth-minted credentials deliberately use ADR 0029's family-level entitlement
commands instead; these tests pin the older static-credential operation.

Against the database, because the operations worth pinning are all about a row:
that a revoked credential is refused rather than silently widened, that a
no-op says so instead of writing, and that the scope column ends up holding
what the operator asked for.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select

from app.vault.auth import VaultScope, hash_secret
from app.vault.tables import vault_agent_credentials
from scripts.issue_vault_credential import grant, revoke_scope
from tests.vault.test_search import vault_service


PRINCIPAL_PREFIX = "test-scopes-"


def _run(coroutine_factory):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await coroutine_factory(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _seed(
    scopes: tuple[str, ...] = (VaultScope.READ, VaultScope.WRITE),
    *,
    revoked: bool = False,
    expires_at: datetime | None = None,
) -> str:
    credential_id = uuid4().hex[:16]

    async def create(connection):
        await connection.execute(
            insert(vault_agent_credentials).values(
                id=credential_id,
                principal_id=f"{PRINCIPAL_PREFIX}{uuid4().hex[:8]}",
                display_name="scope fixture",
                secret_sha256=hash_secret(uuid4().hex),
                scopes=sorted(scopes),
                expires_at=expires_at,
                revoked_at=datetime.now(UTC) if revoked else None,
            )
        )

    _run(create)
    return credential_id


def _scopes(credential_id: str) -> list[str]:
    def read(connection):
        return connection.execute(
            select(vault_agent_credentials.c.scopes).where(
                vault_agent_credentials.c.id == credential_id
            )
        )

    result = _run(read)
    row = result.one_or_none()
    return sorted(row[0]) if row is not None else []


def _cleanup() -> None:
    def remove(connection):
        return connection.execute(
            delete(vault_agent_credentials).where(
                vault_agent_credentials.c.principal_id.like(f"{PRINCIPAL_PREFIX}%")
            )
        )

    _run(remove)


@pytest.fixture(autouse=True)
def clean_credentials(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _grant(credential_id: str, *scopes: str) -> int:
    return asyncio.run(grant(credential_id, list(scopes)))


def _revoke_scope(credential_id: str, *scopes: str) -> int:
    return asyncio.run(revoke_scope(credential_id, list(scopes)))


# --------------------------------------------------------------- grant ----


def test_granting_adds_a_scope_and_leaves_the_others(capsys) -> None:
    credential_id = _seed()

    code = _grant(credential_id, VaultScope.UPDATE)
    out = capsys.readouterr().out

    assert code == 0
    assert _scopes(credential_id) == sorted(
        [VaultScope.READ, VaultScope.WRITE, VaultScope.UPDATE]
    )
    # The operator has to be able to see what changed, not just that it did.
    assert "before" in out
    assert "after" in out


def test_granting_is_additive_rather_than_a_replacement(capsys) -> None:
    """The failure this guards is an operator losing scopes they did not name."""

    credential_id = _seed((VaultScope.READ, VaultScope.WRITE, VaultScope.REVIEW))

    _grant(credential_id, VaultScope.UPDATE)

    assert VaultScope.REVIEW in _scopes(credential_id)


def test_granting_a_scope_already_held_writes_nothing(capsys) -> None:
    credential_id = _seed()

    code = _grant(credential_id, VaultScope.READ)
    out = capsys.readouterr().out

    assert code == 0
    assert "No change" in out
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])


def test_granting_several_scopes_at_once(capsys) -> None:
    credential_id = _seed((VaultScope.READ,))

    _grant(credential_id, VaultScope.WRITE, VaultScope.UPDATE)

    assert _scopes(credential_id) == sorted(
        [VaultScope.READ, VaultScope.WRITE, VaultScope.UPDATE]
    )


# -------------------------------------------------------- revoke-scope ----


def test_revoking_a_scope_narrows_without_revoking_the_credential(capsys) -> None:
    """Narrowing is not revoking: the client keeps working for what remains.

    That is the whole point of having this next to ``revoke`` -- it is what an
    operator wants after granting one scope too many.
    """

    credential_id = _seed((VaultScope.READ, VaultScope.WRITE, VaultScope.DELETE))

    code = _revoke_scope(credential_id, VaultScope.DELETE)

    assert code == 0
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])
    assert _revoked_at(credential_id) is None


def test_revoking_a_scope_not_held_writes_nothing(capsys) -> None:
    credential_id = _seed()

    code = _revoke_scope(credential_id, VaultScope.DELETE)
    out = capsys.readouterr().out

    assert code == 0
    assert "No change" in out
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])


def test_removing_every_scope_is_allowed_and_says_what_it_did(capsys) -> None:
    """Legal, and specifically not the same thing as revoking.

    A credential with no scopes still authenticates; every route then refuses
    it with 403 rather than 401. An operator who meant to revoke needs to be
    told they have not.
    """

    credential_id = _seed()

    code = _revoke_scope(credential_id, VaultScope.READ, VaultScope.WRITE)
    out = capsys.readouterr().out

    assert code == 0
    assert _scopes(credential_id) == []
    assert "holds no scopes" in out
    assert "revoke it" in out
    assert _revoked_at(credential_id) is None


# ------------------------------------------------------------ refusals ----


@pytest.mark.parametrize("operation", ["grant", "revoke-scope"])
def test_an_unknown_scope_is_refused_before_any_write(
    operation: str, capsys
) -> None:
    """Validated against ``KNOWN_SCOPES``, the same list ``issue`` uses.

    The database CHECK would catch a bad grant, but as an integrity error
    naming a constraint. A typo deserves the list of real scopes instead.
    """

    credential_id = _seed()

    code = (
        _grant(credential_id, "vault:invent")
        if operation == "grant"
        else _revoke_scope(credential_id, "vault:invent")
    )
    err = capsys.readouterr().err

    assert code == 2
    assert "Unknown scope" in err
    assert "vault:read" in err
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])


@pytest.mark.parametrize("operation", ["grant", "revoke-scope"])
def test_an_unknown_credential_is_refused(operation: str, capsys) -> None:
    missing = uuid4().hex[:16]

    code = (
        _grant(missing, VaultScope.UPDATE)
        if operation == "grant"
        else _revoke_scope(missing, VaultScope.UPDATE)
    )

    assert code == 1
    assert "No credential with id" in capsys.readouterr().err


def test_a_revoked_credential_is_refused_rather_than_widened(capsys) -> None:
    """Scopes on a revoked row grant nothing, so widening one is a mistake.

    Refused rather than allowed-but-useless, because the operator doing it is
    plausibly reaching for something that would un-revoke the credential, and
    silence would let them believe it had.
    """

    credential_id = _seed(revoked=True)

    code = _grant(credential_id, VaultScope.UPDATE)
    err = capsys.readouterr().err

    assert code == 1
    assert "is revoked" in err
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])


def test_an_expired_credential_is_refused(capsys) -> None:
    credential_id = _seed(expires_at=datetime.now(UTC) - timedelta(seconds=1))

    code = _grant(credential_id, VaultScope.UPDATE)
    err = capsys.readouterr().err

    assert code == 1
    assert "expired" in err
    assert _scopes(credential_id) == sorted([VaultScope.READ, VaultScope.WRITE])


def test_a_credential_expiring_in_the_future_is_not_refused() -> None:
    """The expiry check is about *expired*, not about *having an expiry*."""

    credential_id = _seed(expires_at=datetime.now(UTC) + timedelta(days=1))

    assert _grant(credential_id, VaultScope.UPDATE) == 0
    assert VaultScope.UPDATE in _scopes(credential_id)


def test_the_secret_is_untouched_by_a_scope_change() -> None:
    """What makes this different from revoke-then-issue: the client keeps working.

    A rotation would hand the operator a new token to distribute; the whole
    point here is that nothing has to be redistributed.
    """

    credential_id = _seed()
    before = _secret(credential_id)

    _grant(credential_id, VaultScope.UPDATE)

    assert _secret(credential_id) == before


# ------------------------------------------------------------- helpers ----


def _revoked_at(credential_id: str):
    def read(connection):
        return connection.execute(
            select(vault_agent_credentials.c.revoked_at).where(
                vault_agent_credentials.c.id == credential_id
            )
        )

    return _run(read).scalar_one()


def _secret(credential_id: str) -> bytes:
    def read(connection):
        return connection.execute(
            select(vault_agent_credentials.c.secret_sha256).where(
                vault_agent_credentials.c.id == credential_id
            )
        )

    return bytes(_run(read).scalar_one())
