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
    Widen static: python -m scripts.issue_vault_credential grant --id <credential-id> --scopes vault:update
    Entitle OAuth: python -m scripts.issue_vault_credential grant-oauth --id <credential-id> --scopes vault:update
    Name OAuth:    python -m scripts.issue_vault_credential label --id <credential-id> --label "laptop review console"

`grant` and `revoke-scope` adjust static credentials. OAuth authority belongs
to its refresh family instead: `grant-oauth` and `revoke-oauth-scope` update the
durable grant and the live credential together, so rotation preserves the
operator's decision without letting the client request it. `label` names one of
those authorizations for display; it is not an identifier and nothing resolves a
credential by it.

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
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from app.env import load_environment
from app.vault.auth import TOKEN_PREFIX, VaultScope, hash_secret
from app.vault.constants import OAUTH_OPERATOR_ENTITLEMENT_SCOPES
from app.vault.db import create_vault_engine, describe_database
from app.vault.repository import (
    VaultAuditEventRepository,
    VaultOAuthGrantRepository,
    VaultOAuthRefreshTokenRepository,
)
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import (
    vault_agent_credentials,
    vault_oauth_grants,
    vault_oauth_refresh_tokens,
)


# Order is the order an operator reads them in, so the three write verbs sit
# together: WRITE is contribute alone, and UPDATE and DELETE are granted
# separately on purpose (ADR 0020). Granting all three reproduces the old
# vault:write, which is a decision rather than a default.
KNOWN_SCOPES = (
    VaultScope.READ,
    VaultScope.WRITE,
    VaultScope.PROPOSE,
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

# Mirrors the CHECK added by vault migration 0019. Enforced here too so a label
# one character too long is a message rather than an IntegrityError.
MAX_LABEL_LENGTH = 120

# How much of a label `list` shows. Longer ones are for the operator's benefit
# in prose; the column exists to tell two authorizations apart at a glance.
_LABEL_COLUMN = 24


class _UnsafeOAuthEntitlementCombination(Exception):
    """The requested grant would collapse a separated reviewer role."""


def _transactions() -> tuple[VaultTransactionService, object]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    # A credential is granted against one database and useless against
    # another, so an operator issuing one has to see which.
    print(f"database   : {describe_database(settings.database_url)}")
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


def _label_of_credential():
    """The operator label on the OAuth family a credential belongs to.

    A correlated subquery rather than a join: a credential should have exactly
    one refresh row, and a join that met a second one would silently list the
    credential twice. Newest first, matching
    ``client_and_family_for_credential``. Static credentials belong to no
    family and correlate to NULL, which is the honest answer -- the label lives
    on the authorization, and they have none.
    """

    return (
        select(vault_oauth_grants.c.label)
        .select_from(
            vault_oauth_refresh_tokens.join(
                vault_oauth_grants,
                vault_oauth_grants.c.family_id
                == vault_oauth_refresh_tokens.c.family_id,
            )
        )
        .where(
            vault_oauth_refresh_tokens.c.credential_id
            == vault_agent_credentials.c.id
        )
        .order_by(vault_oauth_refresh_tokens.c.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )


def _fit(value: str, width: int) -> str:
    """Truncate to the column, marking that something was cut."""

    return value if len(value) <= width else value[: width - 3] + "..."


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
                    _label_of_credential().label("label"),
                ).order_by(vault_agent_credentials.c.created_at)
            )
            rows = list(result.mappings())
    finally:
        await engine.dispose()

    if not rows:
        print("No credentials issued.")
        return 0

    now = datetime.now(UTC)
    # Label before principal, because an OAuth principal is `oauth-<uuid4>` and
    # overruns its column -- everything to its right is ragged already, and the
    # column that exists to be scanned should not be.
    print(
        f"{'id':<20}{'label':<{_LABEL_COLUMN}}{'principal':<22}"
        f"{'state':<10}{'last used':<22}scopes"
    )
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
        label = _fit(row["label"] or "-", _LABEL_COLUMN - 1)
        print(
            f"{row['id']:<20}{label:<{_LABEL_COLUMN}}"
            f"{row['principal_id']:<22}{state:<10}"
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

            oauth_family = await connection.execute(
                select(vault_oauth_refresh_tokens.c.family_id)
                .where(vault_oauth_refresh_tokens.c.credential_id == credential_id)
                .limit(1)
            )
            if oauth_family.scalar_one_or_none() is not None:
                print(
                    f"{credential_id} is OAuth-minted. Use grant-oauth or "
                    "revoke-oauth-scope so the change survives rotation.",
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
    """Widen a static credential; OAuth credentials are refused by the helper."""

    return await _adjust_scopes(credential_id, scopes, granting=True)


async def revoke_scope(credential_id: str, scopes: list[str]) -> int:
    """Narrow a credential, without revoking it.

    The counterpart that makes granting reversible without rotation. Reducing a
    credential's reach is not the same as revoking it: the client keeps working
    for what it may still do, which is exactly what an operator wants after
    granting one scope too many.
    """

    return await _adjust_scopes(credential_id, scopes, granting=False)


async def _adjust_oauth_entitlements(
    credential_id: str,
    scopes: list[str],
    *,
    granting: bool,
) -> int:
    """Change operator authority on the OAuth family containing a credential."""

    invalid = sorted(set(scopes) - set(OAUTH_OPERATOR_ENTITLEMENT_SCOPES))
    if invalid:
        print(
            "OAuth entitlements must be operator-only scopes; invalid: "
            f"{', '.join(invalid)}",
            file=sys.stderr,
        )
        print(
            "Entitlement scopes: "
            f"{', '.join(OAUTH_OPERATOR_ENTITLEMENT_SCOPES)}",
            file=sys.stderr,
        )
        return 2

    transactions, engine = _transactions()
    try:
        async with transactions.transaction() as connection:
            refresh = VaultOAuthRefreshTokenRepository()
            identity = await refresh.client_and_family_for_credential(
                connection, credential_id
            )
            if identity is None:
                print(
                    f"{credential_id!r} is not an OAuth-minted credential.",
                    file=sys.stderr,
                )
                return 1
            client_id, family_id = identity
            if not await refresh.live_credential_ids(connection, family_id):
                print(
                    f"OAuth grant {family_id} has no live refresh token. "
                    "Authorize a new session instead.",
                    file=sys.stderr,
                )
                return 1

            adjusted = await VaultOAuthGrantRepository().adjust_entitlements(
                connection,
                family_id,
                scopes,
                granting=granting,
            )
            if adjusted is None:
                print(
                    f"OAuth grant {family_id} does not exist.",
                    file=sys.stderr,
                )
                return 1
            _grant, before, after = adjusted
            effective = set(_grant.authorized_scopes) | set(after)
            if VaultScope.REVIEW in effective and effective != {
                VaultScope.READ,
                VaultScope.REVIEW,
            }:
                raise _UnsafeOAuthEntitlementCombination
            if before != after:
                await VaultAuditEventRepository().record(
                    connection,
                    operation=(
                        "vault.oauth.entitlement.grant"
                        if granting
                        else "vault.oauth.entitlement.revoke"
                    ),
                    outcome="applied",
                    request_id=uuid4().hex,
                    principal_id="operator-cli",
                    target_type="oauth_grant",
                    target_id=str(family_id),
                )
    except _UnsafeOAuthEntitlementCombination:
        print(
            "vault:review requires a separate OAuth authorization holding "
            "exactly vault:read plus vault:review. Authorize a read-only "
            "family before granting review authority.",
            file=sys.stderr,
        )
        return 2
    finally:
        await engine.dispose()

    # The label, so the operator can see which authorization they just widened
    # rather than matching a uuid by hand. It names nothing here: `family_id`
    # above is still what was changed.
    print(f"client id      : {client_id}")
    print(f"grant family   : {family_id}")
    print(f"label          : {_grant.label or '(none)'}")
    print(f"reference cred : {credential_id}")
    print(f"before         : {', '.join(before) or '(none)'}")
    print(f"after          : {', '.join(after) or '(none)'}")
    if before == after:
        print("No change.")
    return 0


async def set_oauth_label(credential_id: str, label: str | None) -> int:
    """Name the OAuth authorization a credential belongs to, or clear the name.

    Addressed by credential id like the entitlement commands, because that is
    what `list` prints and what the console shows -- the family id appears
    nowhere an operator can copy it from. Any credential in the family resolves
    it, including a rotated-away one.

    Deliberately not audited. `vault_audit_events` records changes to what a
    principal may do; a label changes nothing a request is allowed to reach,
    and writing one there would file it under governance and invite a reader to
    treat it as identity. The label's whole safety claim is that it is display
    only (ADR 0040).

    A dead family is labelled without complaint, unlike `grant-oauth`, which
    refuses one. Widening an authorization nothing can present is a misfire
    worth stopping; naming one so the operator can tell it apart in `list` is
    the ordinary reason to be reading history at all.
    """

    if label is not None and len(label.strip()) > MAX_LABEL_LENGTH:
        print(
            f"Label is {len(label.strip())} characters; the maximum is "
            f"{MAX_LABEL_LENGTH}.",
            file=sys.stderr,
        )
        return 2

    transactions, engine = _transactions()
    try:
        async with transactions.transaction() as connection:
            refresh = VaultOAuthRefreshTokenRepository()
            identity = await refresh.client_and_family_for_credential(
                connection, credential_id
            )
            if identity is None:
                print(
                    f"{credential_id!r} is not an OAuth-minted credential. "
                    "Labels belong to OAuth authorizations; a static "
                    "credential is named by the --name it was issued with.",
                    file=sys.stderr,
                )
                return 1
            client_id, family_id = identity

            grants = VaultOAuthGrantRepository()
            existing = await grants.get(connection, family_id, for_update=True)
            if existing is None:
                print(
                    f"OAuth grant {family_id} does not exist.",
                    file=sys.stderr,
                )
                return 1
            before = existing.label
            updated = await grants.set_label(connection, family_id, label)
            assert updated is not None  # locked above; it cannot vanish here
            after = updated.label
    finally:
        await engine.dispose()

    print(f"client id      : {client_id}")
    print(f"grant family   : {family_id}")
    print(f"reference cred : {credential_id}")
    print(f"before         : {before or '(none)'}")
    print(f"after          : {after or '(none)'}")
    if before == after:
        print("No change.")
    return 0


async def grant_oauth(credential_id: str, scopes: list[str]) -> int:
    return await _adjust_oauth_entitlements(
        credential_id, scopes, granting=True
    )


async def revoke_oauth_scope(credential_id: str, scopes: list[str]) -> int:
    return await _adjust_oauth_entitlements(
        credential_id, scopes, granting=False
    )


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
    # supported only for static credentials. OAuth families use the explicit
    # commands below so an operator can see the persistence boundary.
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

    grant_oauth_parser = commands.add_parser(
        "grant-oauth",
        help="Persistently entitle one OAuth authorization across refresh.",
    )
    grant_oauth_parser.add_argument("--id", required=True, dest="credential_id")
    grant_oauth_parser.add_argument("--scopes", nargs="+", required=True)

    revoke_oauth_parser = commands.add_parser(
        "revoke-oauth-scope",
        help="Remove persistent authority from one OAuth authorization.",
    )
    revoke_oauth_parser.add_argument("--id", required=True, dest="credential_id")
    revoke_oauth_parser.add_argument("--scopes", nargs="+", required=True)

    # Setting and clearing are one subcommand with a required choice, rather
    # than the two verbs the scope commands use. There the asymmetry is the
    # point -- granting and revoking authority are not equally consequential --
    # and here there is none to record: both are display text.
    label_parser = commands.add_parser(
        "label",
        help="Name one OAuth authorization for display, or clear its name.",
    )
    label_parser.add_argument("--id", required=True, dest="credential_id")
    naming = label_parser.add_mutually_exclusive_group(required=True)
    naming.add_argument(
        "--label",
        help=(
            "Operator-facing name, at most "
            f"{MAX_LABEL_LENGTH} characters. Display only: it never resolves "
            "a credential and need not be unique."
        ),
    )
    naming.add_argument(
        "--clear",
        action="store_true",
        help="Remove the label, leaving the authorization unnamed.",
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
    elif arguments.command == "grant-oauth":
        coroutine = grant_oauth(arguments.credential_id, arguments.scopes)
    elif arguments.command == "revoke-oauth-scope":
        coroutine = revoke_oauth_scope(arguments.credential_id, arguments.scopes)
    elif arguments.command == "label":
        coroutine = set_oauth_label(
            arguments.credential_id,
            None if arguments.clear else arguments.label,
        )
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
