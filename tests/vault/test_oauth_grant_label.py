"""Naming an OAuth authorization without making the name an identifier.

The label exists because an operator reading `issue_vault_credential list` sees
`oauth-<uuid4>` repeated. What these tests pin is the boundary in vault ADR
0040: the label is display text on the *family*, so it survives rotation, may
collide freely, and resolves nothing. A test that started treating it as a
handle -- looking a family up by name, refusing a duplicate -- would be
asserting the opposite of the decision.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update

from app.vault.auth import VaultScope, hash_secret
from app.vault.oauth import PRINCIPAL_PREFIX
from app.vault.tables import (
    vault_agent_credentials,
    vault_oauth_clients,
    vault_oauth_grants,
    vault_oauth_refresh_tokens,
)
from scripts.issue_vault_credential import (
    grant_oauth,
    list_credentials,
    set_oauth_label,
)
from tests.vault.test_search import vault_service


CLIENT_PREFIX = "test-label-"
OAUTH_PRINCIPAL = f"{PRINCIPAL_PREFIX}test-label"
STATIC_PRINCIPAL = "test-label-operator"


def _run(factory):
    async def exercise():
        transactions, engine = vault_service()
        try:
            async with transactions.transaction() as connection:
                return await factory(connection)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


def _credential(principal_id: str) -> str:
    credential_id = uuid4().hex[:16]

    async def create(connection):
        await connection.execute(
            insert(vault_agent_credentials).values(
                id=credential_id,
                principal_id=principal_id,
                display_name="label fixture",
                secret_sha256=hash_secret(uuid4().hex),
                scopes=[VaultScope.READ],
            )
        )

    _run(create)
    return credential_id


def _family(*, label: str | None = None) -> tuple[UUID, str]:
    """One authorization with one live access credential, as OAuth mints it."""

    family_id = uuid4()
    client_id = f"{CLIENT_PREFIX}{uuid4().hex}"
    credential_id = _credential(OAUTH_PRINCIPAL)

    async def seed(connection):
        await connection.execute(
            insert(vault_oauth_clients).values(
                client_id=client_id, client_info={"client_id": client_id}
            )
        )
        await connection.execute(
            insert(vault_oauth_grants).values(
                family_id=family_id,
                client_id=client_id,
                authorized_scopes=[VaultScope.READ],
                entitled_scopes=[],
                label=label,
            )
        )
        await connection.execute(
            insert(vault_oauth_refresh_tokens).values(
                token_sha256=uuid4().bytes + uuid4().bytes,
                family_id=family_id,
                client_id=client_id,
                credential_id=credential_id,
                scopes=[VaultScope.READ],
                expires_at=text("now() + interval '10 days'"),
            )
        )

    _run(seed)
    return family_id, credential_id


def _rotate(family_id: UUID) -> str:
    """Mint the next credential in an existing family, as a refresh does."""

    credential_id = _credential(OAUTH_PRINCIPAL)

    async def seed(connection):
        client_id = (
            await connection.execute(
                select(vault_oauth_grants.c.client_id).where(
                    vault_oauth_grants.c.family_id == family_id
                )
            )
        ).scalar_one()
        await connection.execute(
            insert(vault_oauth_refresh_tokens).values(
                token_sha256=uuid4().bytes + uuid4().bytes,
                family_id=family_id,
                client_id=client_id,
                credential_id=credential_id,
                scopes=[VaultScope.READ],
                expires_at=text("now() + interval '10 days'"),
                created_at=text("now() + interval '1 minute'"),
            )
        )

    _run(seed)
    return credential_id


def _grant(family_id: UUID):
    def read(connection):
        return connection.execute(
            select(vault_oauth_grants).where(
                vault_oauth_grants.c.family_id == family_id
            )
        )

    return _run(read).mappings().one()


def _cleanup() -> None:
    async def remove(connection):
        await connection.execute(
            delete(vault_oauth_refresh_tokens).where(
                vault_oauth_refresh_tokens.c.client_id.like(f"{CLIENT_PREFIX}%")
            )
        )
        await connection.execute(
            delete(vault_oauth_grants).where(
                vault_oauth_grants.c.client_id.like(f"{CLIENT_PREFIX}%")
            )
        )
        await connection.execute(
            delete(vault_oauth_clients).where(
                vault_oauth_clients.c.client_id.like(f"{CLIENT_PREFIX}%")
            )
        )
        await connection.execute(
            delete(vault_agent_credentials).where(
                vault_agent_credentials.c.principal_id.in_(
                    [OAUTH_PRINCIPAL, STATIC_PRINCIPAL]
                )
            )
        )

    _run(remove)


@pytest.fixture(autouse=True)
def clean(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _label(credential_id: str, label: str | None) -> int:
    return asyncio.run(set_oauth_label(credential_id, label))


def test_labelling_names_the_authorization_a_credential_belongs_to(
    capsys,
) -> None:
    family_id, credential_id = _family()

    assert _label(credential_id, "laptop review console") == 0

    assert _grant(family_id)["label"] == "laptop review console"
    output = capsys.readouterr().out
    assert "before         : (none)" in output
    assert "after          : laptop review console" in output


def test_a_rotated_away_credential_still_names_its_family() -> None:
    """The id is a lookup handle, not the target.

    An operator reads a credential id out of `list` or the console header, and
    the family has usually rotated since. If only the newest credential
    resolved, labelling would fail exactly when the family is old enough to be
    worth telling apart.
    """

    family_id, first = _family()
    _rotate(family_id)

    assert _label(first, "importer") == 0

    assert _grant(family_id)["label"] == "importer"


def test_the_label_survives_rotation() -> None:
    """It is on the family, so a new credential inherits it with no copying."""

    family_id, credential_id = _family()
    _label(credential_id, "importer")

    rotated = _rotate(family_id)

    assert _grant(family_id)["label"] == "importer"
    assert _label(rotated, "importer") == 0  # the same family, still named


def test_clearing_leaves_the_authorization_unnamed(capsys) -> None:
    family_id, credential_id = _family(label="mistaken")

    assert _label(credential_id, None) == 0

    assert _grant(family_id)["label"] is None
    assert "after          : (none)" in capsys.readouterr().out


def test_a_blank_label_clears_rather_than_storing_an_empty_string() -> None:
    """One representation of absent, so no reader has to know about two."""

    family_id, credential_id = _family(label="mistaken")

    assert _label(credential_id, "   ") == 0

    assert _grant(family_id)["label"] is None


def test_a_label_is_stored_without_surrounding_whitespace() -> None:
    family_id, credential_id = _family()

    _label(credential_id, "  laptop  ")

    assert _grant(family_id)["label"] == "laptop"


def test_two_authorizations_may_share_a_label() -> None:
    """Not an oversight.

    A label that had to be unique would be a second identifier, which is the
    mistake ADR 0040 is shaped to avoid: the uuid distinguishes two families
    and always did.
    """

    first_family, first_credential = _family()
    second_family, second_credential = _family()

    assert _label(first_credential, "laptop") == 0
    assert _label(second_credential, "laptop") == 0

    assert _grant(first_family)["label"] == "laptop"
    assert _grant(second_family)["label"] == "laptop"
    assert first_family != second_family


def test_relabelling_reports_no_change_when_the_name_is_the_same(
    capsys,
) -> None:
    _family_id, credential_id = _family(label="laptop")

    assert _label(credential_id, "laptop") == 0

    assert "No change." in capsys.readouterr().out


def test_labelling_changes_no_authority() -> None:
    """The claim the whole design rests on, asserted rather than assumed."""

    family_id, credential_id = _family()
    before = _grant(family_id)

    _label(credential_id, "laptop")
    after = _grant(family_id)

    assert after["authorized_scopes"] == before["authorized_scopes"]
    assert after["entitled_scopes"] == before["entitled_scopes"]

    def read_scopes(connection):
        return connection.execute(
            select(vault_agent_credentials.c.scopes).where(
                vault_agent_credentials.c.id == credential_id
            )
        )

    assert _run(read_scopes).scalar_one() == [VaultScope.READ]


def test_a_label_over_the_maximum_is_refused_before_any_write(capsys) -> None:
    family_id, credential_id = _family()

    assert _label(credential_id, "x" * 121) == 2

    assert _grant(family_id)["label"] is None
    assert "the maximum is 120" in capsys.readouterr().err


def test_a_label_at_the_maximum_is_accepted() -> None:
    family_id, credential_id = _family()

    assert _label(credential_id, "x" * 120) == 0

    assert _grant(family_id)["label"] == "x" * 120


def test_a_static_credential_is_refused(capsys) -> None:
    """Labels belong to authorizations.

    A static credential has a display name already, chosen when it was issued,
    and no family to hang this on.
    """

    static = _credential(STATIC_PRINCIPAL)

    assert _label(static, "importer") == 1

    assert "not an OAuth-minted credential" in capsys.readouterr().err


def test_an_unknown_credential_is_refused(capsys) -> None:
    assert _label("no-such-credential", "importer") == 1

    assert "not an OAuth-minted credential" in capsys.readouterr().err


def test_list_shows_the_label_beside_the_credential(capsys) -> None:
    _family_id, oauth_credential = _family()
    static = _credential(STATIC_PRINCIPAL)
    _label(oauth_credential, "laptop review console")
    capsys.readouterr()

    assert asyncio.run(list_credentials()) == 0

    lines = capsys.readouterr().out.splitlines()
    oauth_line = next(line for line in lines if line.startswith(oauth_credential))
    static_line = next(line for line in lines if line.startswith(static))
    assert "laptop review console" in oauth_line
    # An unlabelled row reads as unlabelled rather than as a gap in the table.
    assert "-" in static_line.removeprefix(static)[:24]


def test_list_marks_a_label_it_had_to_truncate(capsys) -> None:
    """Cut labels say so, so a truncated one is not read as the whole name."""

    _family_id, credential_id = _family()
    _label(credential_id, "the importer that runs on the media box")
    capsys.readouterr()

    assert asyncio.run(list_credentials()) == 0

    line = next(
        row
        for row in capsys.readouterr().out.splitlines()
        if row.startswith(credential_id)
    )
    assert "the importer that ru..." in line


def test_the_entitlement_commands_name_what_they_widen(capsys) -> None:
    """The reason to have a label at all, from an operator's side.

    `grant-oauth` used to echo a uuid back, which told the operator what they
    had typed rather than what they had changed.
    """

    _family_id, credential_id = _family(label="the importer")
    capsys.readouterr()

    assert asyncio.run(grant_oauth(credential_id, [VaultScope.UPDATE])) == 0

    assert "label          : the importer" in capsys.readouterr().out


def test_the_column_refuses_an_empty_label() -> None:
    """The normalisation in the repository is a convenience; this is the rule.

    Migration 0019's CHECK is what makes NULL the only spelling of absent, so
    a future writer that skips the repository cannot introduce a second one.
    """

    family_id, _credential_id = _family()

    async def blank(connection):
        await connection.execute(
            update(vault_oauth_grants)
            .where(vault_oauth_grants.c.family_id == family_id)
            .values(label="")
        )

    with pytest.raises(Exception, match="vault_oauth_grants_label_shape"):
        _run(blank)
