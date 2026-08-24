"""The operator password, which is bcrypt and not the vault's usual SHA-256.

``auth.hash_secret`` hashes agent secrets with plain SHA-256 because they are
machine-generated with full entropy. This module exists because that reasoning
does not transfer to a password a person chose, and these tests pin the
difference rather than the bcrypt library's behaviour.

No database. The hash is configuration, not a row.
"""

import asyncio
import logging

import pytest

from app.vault.auth import hash_secret
from app.vault.passwords import (
    MAX_PASSWORD_BYTES,
    PasswordTooLong,
    hash_password,
    verify_password,
)
from app.vault.settings import operator_password_hash


PASSWORD = "correct horse battery staple"


def test_a_password_verifies_against_its_own_hash() -> None:
    hashed = asyncio.run(hash_password(PASSWORD))

    assert asyncio.run(verify_password(PASSWORD, hashed)) is True
    assert asyncio.run(verify_password(PASSWORD + "!", hashed)) is False


def test_two_hashes_of_one_password_differ() -> None:
    """Salted, which is most of what distinguishes this from ``hash_secret``.

    A deterministic digest of a human-chosen password is a lookup key into a
    rainbow table; that is exactly what SHA-256 gives and what bcrypt does not.
    """

    first = asyncio.run(hash_password(PASSWORD))
    second = asyncio.run(hash_password(PASSWORD))

    assert first != second
    assert asyncio.run(verify_password(PASSWORD, first)) is True
    assert asyncio.run(verify_password(PASSWORD, second)) is True


def test_the_hash_is_not_the_sha256_the_rest_of_the_package_uses() -> None:
    """The one assertion that would fail if someone "simplified" this away."""

    hashed = asyncio.run(hash_password(PASSWORD))

    assert hashed.startswith("$2")
    assert hashed.encode("ascii") != hash_secret(PASSWORD)


def test_an_over_long_password_is_refused_rather_than_truncated() -> None:
    """bcrypt silently truncates at 72 bytes, which is a real surprise.

    A passphrase whose first 72 bytes matched would otherwise verify. Hashing
    raises so the operator hears about it once, at the moment they set the
    secret.
    """

    too_long = "a" * (MAX_PASSWORD_BYTES + 1)

    with pytest.raises(PasswordTooLong):
        asyncio.run(hash_password(too_long))


def test_verifying_an_over_long_password_is_a_mismatch_not_a_crash() -> None:
    """One failure message for everything, per ADR 0024.

    A raise here would become a 500 that tells the submitter their input was
    interesting, which is the probe the single message exists to deny.
    """

    hashed = asyncio.run(hash_password(PASSWORD))
    too_long = "a" * (MAX_PASSWORD_BYTES + 1)

    assert asyncio.run(verify_password(too_long, hashed)) is False


def test_a_multibyte_password_is_measured_in_bytes_not_characters() -> None:
    """bcrypt's limit is bytes, so a 3-byte character counts three times."""

    # 24 characters, 72 bytes -- exactly at the limit.
    at_limit = "é" * 36
    assert len(at_limit.encode("utf-8")) == MAX_PASSWORD_BYTES
    hashed = asyncio.run(hash_password(at_limit))
    assert asyncio.run(verify_password(at_limit, hashed)) is True

    with pytest.raises(PasswordTooLong):
        asyncio.run(hash_password("é" * 37))


def test_a_malformed_stored_hash_fails_the_login_and_logs_the_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A truncated config var, or one pasted with a shell's quoting attached.

    Reported as a failed login rather than a crash -- the operator sees what
    they would for a wrong password -- while the log carries the real cause.
    """

    with caplog.at_level(logging.ERROR, logger="app.vault.passwords"):
        assert asyncio.run(verify_password(PASSWORD, "not-a-bcrypt-hash")) is False

    assert "not a valid bcrypt hash" in caplog.text


def test_the_log_never_carries_the_password_or_the_hash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.vault.passwords"):
        asyncio.run(verify_password(PASSWORD, "$2b$12$truncated"))

    assert PASSWORD not in caplog.text
    assert "truncated" not in caplog.text


def test_an_unset_operator_hash_is_none_and_never_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None means "this identity method is not configured", never "anything goes".

    An empty string would be falsy in most checks and a valid-looking value in
    a few, which is the shape of gap this normalization closes.
    """

    monkeypatch.delenv("VAULT_OPERATOR_PASSWORD_HASH", raising=False)
    assert operator_password_hash() is None

    monkeypatch.setenv("VAULT_OPERATOR_PASSWORD_HASH", "   ")
    assert operator_password_hash() is None


def test_a_configured_operator_hash_is_returned_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing whitespace survives a copy-paste and breaks bcrypt silently."""

    hashed = asyncio.run(hash_password(PASSWORD))
    monkeypatch.setenv("VAULT_OPERATOR_PASSWORD_HASH", f"  {hashed}\n")

    resolved = operator_password_hash()

    assert resolved == hashed
    assert asyncio.run(verify_password(PASSWORD, resolved)) is True


def test_hashing_does_not_block_the_event_loop() -> None:
    """bcrypt is CPU-bound with no async variant, so it has to be offloaded.

    The host's ``AGENTS.md`` calls the async/sync boundary the likeliest source
    of subtle bugs here, and a direct call would stall every concurrent request
    on the worker for the full hash duration. Asserted by running another task
    to completion while the hash is in flight, which is only possible if the
    hash left the loop.
    """

    async def exercise() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        ticker = asyncio.create_task(tick())
        await hash_password(PASSWORD)
        ticker.cancel()
        return ticks

    assert asyncio.run(exercise()) > 0
