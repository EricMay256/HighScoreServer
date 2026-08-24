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
    Widen:   python -m scripts.issue_vault_credential grant --id <credential-id> --scopes vault:update
    Narrow:  python -m scripts.issue_vault_credential revoke-scope --id <credential-id> --scopes vault:update

`grant` and `revoke-scope` change what an existing credential may do without
rotating its secret, which is what makes them different from revoke-then-issue.
Vault ADR 0024 requires them: OAuth clients all start at the read+write baseline
and some will need more, and the alternative -- a hand-written UPDATE against
production -- does not survive becoming routine.

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


# Order is the order an operator reads them in, so the three write verbs sit
# together: WRITE is contribute alone, and UPDATE and DELETE are granted
# separately on purpose (ADR 0020). Granting all three reproduces the old
# vault:write, which is a decision rather than a default.
KNOWN_SCOPES = (
    VaultScope.READ,
    VaultScope.WRITE,
    VaultScope.UPDATE,
    VaultScope.DELETE,
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


async def _adjust_scopes(
    credential_id: str,
    scopes: list[str],
    *,
    granting: bool,
) -> int:
    """Add or remove scopes on one credential, printing before and after.

    One function for both verbs because the only difference is a set operation
    and a word. Everything that has to be right -- validating the scope names,
    refusing a credential that cannot use them, reporting a no-op as a no-op,
    and showing the operator what actually changed -- is identical, and two
    copies would eventually disagree about one of them.

    **Read-modify-write inside one transaction.** The scopes column is an
    array, so there is no `scopes = scopes || '{...}'` that is also safe to
    express as a set difference; doing it in Python means the row must not move
    underneath. `FOR UPDATE` on the select is what makes two concurrent grants
    compose rather than one overwriting the other.
    """

    unknown = sorted(set(scopes) - set(KNOWN_SCOPES))
    if unknown:
        print(f"Unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Known scopes: {', '.join(KNOWN_SCOPES)}", file=sys.stderr)
        return 2

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
                )
                .where(vault_agent_credentials.c.id == credential_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                print(
                    f"No credential with id {credential_id!r}.",
                    file=sys.stderr,
                )
                return 1

            # Refused rather than allowed-but-useless. Widening a credential
            # nothing can present is at best confusing and at worst a step
            # somebody takes while thinking it un-revokes one. Narrowing is
            # refused for the same reason in reverse: it reads as protective
            # and achieves nothing, so the honest answer is that the credential
            # is already dead.
            now = datetime.now(UTC)
            if row["revoked_at"] is not None:
                print(
                    f"{credential_id} is revoked. Issue a new credential "
                    "instead; scopes on a revoked row grant nothing.",
                    file=sys.stderr,
                )
                return 1
            if row["expires_at"] is not None and row["expires_at"] <= now:
                print(
                    f"{credential_id} expired at "
                    f"{row['expires_at'].isoformat(timespec='seconds')}. "
                    "Issue a new credential instead.",
                    file=sys.stderr,
                )
                return 1

            before = set(row["scopes"])
            after = before | set(scopes) if granting else before - set(scopes)

            if after == before:
                verb = "already granted" if granting else "not held"
                print(
                    f"No change: {', '.join(sorted(scopes))} {verb} on "
                    f"{credential_id}."
                )
                return 0

            await connection.execute(
                update(vault_agent_credentials)
                .where(vault_agent_credentials.c.id == credential_id)
                .values(scopes=sorted(after))
            )
    finally:
        await engine.dispose()

    print(f"credential id : {credential_id}")
    print(f"principal     : {row['principal_id']}")
    print(f"before        : {', '.join(sorted(before)) or '(none)'}")
    print(f"after         : {', '.join(sorted(after)) or '(none)'}")
    if not after:
        # Legal, and worth saying out loud. A credential with no scopes still
        # authenticates -- every route then refuses it with 403 rather than
        # 401 -- so this is not the same thing as revoking, and an operator who
        # meant to revoke should be told they have not.
        print()
        print(
            "Note: this credential now holds no scopes. It still "
            "authenticates and will be refused per route. To stop it working "
            "at all, revoke it."
        )
    return 0


async def grant(credential_id: str, scopes: list[str]) -> int:
    """Widen a credential. ADR 0024 requires this to exist before OAuth ships.

    Every OAuth client starts at the read+write baseline and some will need
    more; before this the only documented way to widen was a hand-written
    UPDATE against production, which does not survive becoming routine.

    Above-baseline scopes are granted deliberately and never requested, which
    is what makes an operator command the right shape for it: `valid_scopes`
    stops a client asking for `vault:update`, and this is the only way it can
    receive one.
    """

    return await _adjust_scopes(credential_id, scopes, granting=True)


async def revoke_scope(credential_id: str, scopes: list[str]) -> int:
    """Narrow a credential, without revoking it.

    The counterpart that makes granting reversible without rotation. Reducing a
    credential's reach is not the same as revoking it: the client keeps working
    for what it may still do, which is exactly what an operator wants after
    granting one scope too many.
    """

    return await _adjust_scopes(credential_id, scopes, granting=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Issue, list, revoke, and adjust the scopes of vault agent "
            "credentials."
        ),
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

    # Widening and narrowing are separate subcommands rather than one with a
    # sign, because the two are not equally consequential and the command an
    # operator types should say which one they meant. `grant` is also the only
    # supported way an above-baseline scope ever reaches an OAuth client
    # (vault ADR 0024).
    grant_parser = commands.add_parser(
        "grant",
        help="Add scopes to an existing credential.",
    )
    grant_parser.add_argument("--id", required=True, dest="credential_id")
    grant_parser.add_argument(
        "--scopes",
        nargs="+",
        required=True,
        help=f"Scopes to add. Known: {', '.join(KNOWN_SCOPES)}",
    )

    revoke_scope_parser = commands.add_parser(
        "revoke-scope",
        help="Remove scopes from a credential, without revoking it.",
    )
    revoke_scope_parser.add_argument("--id", required=True, dest="credential_id")
    revoke_scope_parser.add_argument(
        "--scopes",
        nargs="+",
        required=True,
        help=f"Scopes to remove. Known: {', '.join(KNOWN_SCOPES)}",
    )

    arguments = parser.parse_args()
    load_environment()

    if arguments.command == "issue":
        if arguments.days is not None and arguments.days < 1:
            parser.error("--days must be at least 1")
        coroutine = issue(arguments.name, arguments.scopes, arguments.days)
    elif arguments.command == "list":
        coroutine = list_credentials()
    elif arguments.command == "grant":
        coroutine = grant(arguments.credential_id, arguments.scopes)
    elif arguments.command == "revoke-scope":
        coroutine = revoke_scope(arguments.credential_id, arguments.scopes)
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
