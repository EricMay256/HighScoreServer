"""Human password hashing for the operator login page.

The vault's own, and duplicated on purpose. HSS's ``app/auth.py`` already has
``hash_password``/``verify_password`` with identical bodies, but ``app/vault/``
may contain no ``from app.`` -- that rule is what keeps extraction a directory
move rather than a find-and-replace, and ``tests/vault/test_boundaries.py``
asserts it. Ten lines of bcrypt is a cheaper price than a host dependency the
package cannot take with it. ``bcrypt`` is listed in the extraction manifest as
a dependency that stays in both repositories.

**bcrypt, not SHA-256, and the distinction is the whole reason this file
exists.** ``auth.py`` hashes agent secrets with plain SHA-256 because they are
machine-generated with full entropy: there is no dictionary to slow down, and a
read surface cannot afford a deliberately slow hash per request. An operator
password is chosen by a person, so a work factor is exactly what is wanted, and
this runs once per authorization rather than once per request. ADR 0015 says not
to carry the SHA-256 reasoning to human-chosen passwords; ADR 0024 is where that
becomes concrete.

**The hash never reaches this database.** It is configuration
(``VAULT_OPERATOR_PASSWORD_HASH``), not a row, because there is exactly one and
it has no lifecycle a table would model -- and because a database's backups
circulate more widely than a config var. Rotating it is
``heroku config:set``, which is also the revocation story.

**Offloaded to a thread.** bcrypt is CPU-bound by design and has no async
variant, so a direct call inside a handler blocks the event loop for the full
hash duration. Its C implementation releases the GIL while hashing, so
``asyncio.to_thread`` genuinely moves the work to another core rather than
merely yielding. This is the async/sync boundary the host's ``AGENTS.md`` warns
is the likeliest source of subtle bugs here.
"""

import asyncio
import logging

import bcrypt


logger = logging.getLogger(__name__)

# What bcrypt itself accepts before truncating. Longer input is rejected rather
# than silently shortened: a passphrase whose first 72 bytes match would
# otherwise verify, which is a surprise nobody should meet at a login form.
MAX_PASSWORD_BYTES = 72


class PasswordTooLong(ValueError):
    """The password exceeds what bcrypt can hash without truncation."""


def _check_length(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(
            f"password exceeds {MAX_PASSWORD_BYTES} bytes once UTF-8 encoded"
        )
    return encoded


def _hash_sync(password: str) -> str:
    return bcrypt.hashpw(_check_length(password), bcrypt.gensalt()).decode("ascii")


def _verify_sync(password: str, hashed: str) -> bool:
    try:
        encoded = _check_length(password)
    except PasswordTooLong:
        # A submitted password that cannot be hashed cannot match one that was.
        # Returned as a mismatch rather than raised, because the caller renders
        # one message for every failure (ADR 0024) and an exception here would
        # become a 500 that says the input was interesting.
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        # A malformed stored hash -- a truncated config var, a value pasted
        # with a shell's quoting still attached. Logged as a *type*, never with
        # the value, and reported as a failed login rather than a crash: the
        # operator sees the same message they would for a wrong password, and
        # the log is where the real cause is findable.
        logger.error("vault operator password hash is not a valid bcrypt hash")
        return False


async def hash_password(password: str) -> str:
    """Hash an operator password. Used by tooling, never on a request path."""

    return await asyncio.to_thread(_hash_sync, password)


async def verify_password(password: str, hashed: str) -> bool:
    """Constant-time verification against a stored bcrypt hash.

    False for every kind of failure -- wrong password, over-long input,
    unparseable stored hash -- because the login page renders one message for
    all of them and this is where that starts.
    """

    return await asyncio.to_thread(_verify_sync, password, hashed)
