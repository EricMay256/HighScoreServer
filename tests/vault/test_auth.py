"""Token parsing, secret comparison, and scope checks."""

from datetime import UTC, datetime, timedelta

import pytest

from app.vault.auth import (
    TOKEN_PREFIX,
    VaultCredential,
    VaultScope,
    authorize,
    hash_secret,
    parse_token,
    secret_matches,
)


SECRET = "a" * 64


def credential(**overrides) -> VaultCredential:
    base = {
        "id": "abcdef0123456789",
        "principal_id": "agent:test",
        "display_name": "test credential",
        "secret_sha256": hash_secret(SECRET),
        "scopes": (VaultScope.READ,),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return VaultCredential(**{**base, **overrides})


def test_a_credential_id_may_contain_underscores() -> None:
    """The token is split from the right, and this is why.

    ``vault_agent_credentials_id_format`` permits '_' in the id, so splitting
    from the left would truncate any id containing one and silently fail to
    authenticate a perfectly valid credential.
    """

    parsed = parse_token(f"{TOKEN_PREFIX}_agent_one_two_{SECRET}")

    assert parsed is not None
    assert parsed.credential_id == "agent_one_two"
    assert parsed.secret == SECRET


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        f"wrongprefix_abcdef0123456789_{SECRET}",
        # No separator between id and secret.
        f"{TOKEN_PREFIX}_abcdef0123456789{SECRET}",
        # Id too short for the schema's 8-character minimum.
        f"{TOKEN_PREFIX}_short_{SECRET}",
        # Secret outside the hex alphabet, which is what keeps the split
        # unambiguous.
        f"{TOKEN_PREFIX}_abcdef0123456789_not-hex-at-all",
        f"{TOKEN_PREFIX}_abcdef0123456789_",
    ],
)
def test_malformed_tokens_do_not_parse(token: str) -> None:
    assert parse_token(token) is None


def test_a_lookup_miss_still_compares_a_secret() -> None:
    """An unknown credential ID must cost the same work as a wrong secret.

    Returning early on a miss would let response timing enumerate valid
    credential IDs.
    """

    assert secret_matches(None, SECRET) is False
    assert secret_matches(credential(), SECRET) is True
    assert secret_matches(credential(), "b" * 64) is False


def test_authorize_accepts_an_active_credential_with_the_scope() -> None:
    assert authorize(credential(), SECRET, (VaultScope.READ,)) is None


def test_authorize_separates_a_bad_secret_from_a_missing_scope() -> None:
    """The two map to different HTTP statuses, so they cannot be one reason."""

    assert authorize(credential(), "b" * 64, (VaultScope.READ,)) == "invalid"
    assert authorize(None, SECRET, (VaultScope.READ,)) == "invalid"
    assert (
        authorize(credential(scopes=(VaultScope.EXPORT,)), SECRET, (VaultScope.READ,))
        == "scope"
    )


def test_revoked_and_expired_credentials_are_inactive() -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)

    assert authorize(credential(revoked_at=past), SECRET, ()) == "inactive"
    assert authorize(credential(expires_at=past), SECRET, ()) == "inactive"


def test_a_future_expiry_is_still_active() -> None:
    future = datetime.now(UTC) + timedelta(days=1)

    assert credential(expires_at=future).is_active() is True
    assert authorize(credential(expires_at=future), SECRET, ()) is None


def test_a_future_dated_revocation_is_already_inactive() -> None:
    future = datetime.now(UTC) + timedelta(days=1)

    assert credential(revoked_at=future).is_active() is False
    assert authorize(credential(revoked_at=future), SECRET, ()) == "inactive"


def test_every_required_scope_must_be_present() -> None:
    both = credential(scopes=(VaultScope.READ, VaultScope.EXPORT))

    assert authorize(both, SECRET, (VaultScope.READ, VaultScope.EXPORT)) is None
    assert (
        authorize(both, SECRET, (VaultScope.READ, VaultScope.WRITE)) == "scope"
    )


def test_hashing_is_stable_and_the_width_the_column_expects() -> None:
    assert hash_secret(SECRET) == hash_secret(SECRET)
    assert hash_secret(SECRET) != hash_secret("b" * 64)
    # vault_agent_credentials_sha256_length
    assert len(hash_secret(SECRET)) == 32
