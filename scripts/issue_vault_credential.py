"""
Issues, lists, and revokes vault agent credentials.

The vault read surface authenticates operator-issued tokens of the form
``hssv1_<credential-id>_<secret>``. Only the SHA-256 of the secret is stored,
so **the token is printed once and cannot be recovered** — if it is lost,
revoke the credential and issue another.

Usage:
    Issue:   python -m scripts.issue_vault_credential issue --name "claude-code" --scopes vault:read
    List:    python -m scripts.issue_vault_credential list
    Revoke:  python -m scripts.issue_vault_credential revoke --id <credential-id>

Environment variables:
    DATABASE_URL   Required. The vault schema must already be migrated.
                   VAULT_DATABASE_URL takes precedence when set.

Against production, set the URL explicitly for the command rather than relying
on .env — issuing a credential into the wrong database is silent.
"""

import argparse
import asyncio
import secrets
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update

from app.env import load_environment
from app.vault.auth import TOKEN_PREFIX, VaultScope, hash_secret
from app.vault.db import create_vault_engine
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_agent_credentials


KNOWN_SCOPES = (
    VaultScope.READ,
    VaultScope.WRITE,
    VaultScope.REVIEW,
    VaultScope.COMPILE,
    VaultScope.EXPORT,
)

# 32 hex characters of the id keeps it inside the schema's 8..64 limit while
# leaving no realistic chance of collision. The secret is hex so that the final
# '_' in a token is unambiguously the separator (ids may contain '_').
_ID_BYTES = 8
_SECRET_BYTES = 32


def _transactions() -> tuple[VaultTransactionService, object]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    engine, observer = create_vault_engine(settings)
    return VaultTransactionService(engine, observer), engine


async def issue(name: str, scopes: list[str], days: int | None) -> int:
    unknown = sorted(set(scopes) - set(KNOWN_SCOPES))
    if unknown:
        print(f"Unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Known scopes: {', '.join(KNOWN_SCOPES)}", file=sys.stderr)
        return 2

    credential_id = secrets.token_hex(_ID_BYTES)
    secret = secrets.token_hex(_SECRET_BYTES)
    expires_at = (
        datetime.now(UTC) + timedelta(days=days) if days is not None else None
    )

    transactions, engine = _transactions()
    try:
        async with transactions.transaction() as connection:
            await connection.execute(
                insert(vault_agent_credentials).values(
                    id=credential_id,
                    # One principal per credential by default. Reissuing for
                    # the same agent should pass the existing principal so
                    # rotation does not look like a new actor in the audit log.
                    principal_id=name,
                    display_name=name,
                    secret_sha256=hash_secret(secret),
                    scopes=sorted(set(scopes)),
                    expires_at=expires_at,
                )
            )
    finally:
        await engine.dispose()

    print(f"credential id : {credential_id}")
    print(f"principal     : {name}")
    print(f"scopes        : {', '.join(sorted(set(scopes)))}")
    print(f"expires       : {expires_at.isoformat() if expires_at else 'never'}")
    print()
    print("Token (shown once, not recoverable):")
    print(f"  {TOKEN_PREFIX}_{credential_id}_{secret}")
    return 0


async def list_credentials() -> int:
    transactions, engine = _transactions()
    try:
        async with transactions.transaction() as connection:
            result = await connection.execute(
                select(
                    vault_agent_credentials.c.id,
                    vault_agent_credentials.c.principal_id,
                    vault_agent_credentials.c.scopes,
                    vault_agent_credentials.c.expires_at,
                    vault_agent_credentials.c.revoked_at,
                    vault_agent_credentials.c.last_used_at,
                ).order_by(vault_agent_credentials.c.created_at)
            )
            rows = list(result.mappings())
    finally:
        await engine.dispose()

    if not rows:
        print("No credentials issued.")
        return 0

    now = datetime.now(UTC)
    print(f"{'id':<20}{'principal':<22}{'state':<10}{'last used':<22}scopes")
    for row in rows:
        if row["revoked_at"] is not None:
            state = "revoked"
        elif row["expires_at"] is not None and row["expires_at"] <= now:
            state = "expired"
        else:
            state = "active"
        last_used = (
            row["last_used_at"].isoformat(timespec="seconds")
            if row["last_used_at"]
            else "never"
        )
        print(
            f"{row['id']:<20}{row['principal_id']:<22}{state:<10}"
            f"{last_used:<22}{', '.join(row['scopes'])}"
        )
    return 0


async def revoke(credential_id: str) -> int:
    transactions, engine = _transactions()
    try:
        async with transactions.transaction() as connection:
            result = await connection.execute(
                update(vault_agent_credentials)
                .where(
                    vault_agent_credentials.c.id == credential_id,
                    vault_agent_credentials.c.revoked_at.is_(None),
                )
                .values(revoked_at=func.now())
                .returning(vault_agent_credentials.c.id)
            )
            revoked = result.scalar_one_or_none()
    finally:
        await engine.dispose()

    if revoked is None:
        print(
            f"No active credential with id {credential_id!r}.",
            file=sys.stderr,
        )
        return 1
    print(f"Revoked {credential_id}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue, list, and revoke vault agent credentials.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    issue_parser = commands.add_parser("issue", help="Issue a new credential.")
    issue_parser.add_argument("--name", required=True, help="Principal/display name.")
    issue_parser.add_argument(
        "--scopes",
        nargs="+",
        default=[VaultScope.READ],
        help=f"Scopes to grant. Known: {', '.join(KNOWN_SCOPES)}",
    )
    issue_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Expire after this many days. Omit for no expiry.",
    )

    commands.add_parser("list", help="List credentials and their state.")

    revoke_parser = commands.add_parser("revoke", help="Revoke a credential.")
    revoke_parser.add_argument("--id", required=True, dest="credential_id")

    arguments = parser.parse_args()
    load_environment()

    if arguments.command == "issue":
        if arguments.days is not None and arguments.days < 1:
            parser.error("--days must be at least 1")
        coroutine = issue(arguments.name, arguments.scopes, arguments.days)
    elif arguments.command == "list":
        coroutine = list_credentials()
    else:
        coroutine = revoke(arguments.credential_id)

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. run_dev.py,
    # conftest.py, and seed_vault_demo.py all do this. No-op on Linux/Heroku,
    # where SelectorEventLoop is already the default.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
