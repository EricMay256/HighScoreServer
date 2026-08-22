"""
Hashes the vault operator password for ``VAULT_OPERATOR_PASSWORD_HASH``.

The vault's OAuth authorization server (vault ADR 0024) authenticates one human
— the operator approving a client's authorization — against a **bcrypt hash**
held in configuration. This prints that hash. It never stores anything, touches
no database, and needs no ``DATABASE_URL``.

Usage:
    python -m scripts.hash_vault_operator_password

The password is read with ``getpass``, so it is not echoed and does not reach
shell history. That is the whole reason this exists as a script rather than a
documented ``python -c`` one-liner: a one-liner that reads with ``input()``
prints the secret to the terminal, and one that takes it as an argument leaves
it in history and in the process table.

Set the result with the value **single-quoted** — a bcrypt hash starts ``$2b$``
and contains further ``$`` characters that a shell would otherwise expand away,
leaving a mangled config var that reads back as a wrong password:

    heroku config:set VAULT_OPERATOR_PASSWORD_HASH='$2b$12$...' --app high-score-server

bcrypt rather than the SHA-256 the rest of the vault uses on agent secrets:
those are machine-generated with full entropy, so a work factor buys nothing,
while this is a password a person chose and the work factor is the point. Vault
ADR 0015 says explicitly not to carry that reasoning across.
"""

import asyncio
import getpass
import sys

from app.vault.passwords import MAX_PASSWORD_BYTES, PasswordTooLong, hash_password


def read_password() -> str | None:
    """Prompt twice without echo, or None if the two do not match.

    Confirmed because a typo in a write-only field is otherwise discovered at
    the login form, after a deploy, with a message that says only that the
    password was wrong — ADR 0024 renders one failure for every cause, which is
    right for an attacker and unhelpful here.
    """

    password = getpass.getpass("Operator password: ")
    if not password:
        print("Empty password; nothing to hash.", file=sys.stderr)
        return None
    if password != getpass.getpass("Confirm: "):
        print("Passwords do not match.", file=sys.stderr)
        return None
    return password


def main() -> int:
    try:
        password = read_password()
    except (EOFError, KeyboardInterrupt):
        # A piped stdin, or an operator changing their mind. Neither is an
        # error worth a traceback.
        print(file=sys.stderr)
        return 1
    if password is None:
        return 1

    try:
        hashed = asyncio.run(hash_password(password))
    except PasswordTooLong:
        # bcrypt truncates silently at 72 bytes, so a longer passphrase would
        # verify on its prefix alone. Refused here, once, rather than becoming
        # a property of the deployment nobody knows about. The limit is bytes:
        # accented or non-Latin characters reach it sooner than their length
        # suggests.
        print(
            f"Password exceeds {MAX_PASSWORD_BYTES} bytes once UTF-8 encoded. "
            "bcrypt would silently ignore everything past that, so a different "
            "passphrase sharing the first "
            f"{MAX_PASSWORD_BYTES} bytes would also verify. Choose a shorter one.",
            file=sys.stderr,
        )
        return 1

    print()
    print("VAULT_OPERATOR_PASSWORD_HASH (single-quote it when setting):")
    print(f"  {hashed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
