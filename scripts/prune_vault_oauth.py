"""
Prunes the OAuth authorization server's transient state.

Four things accumulate once vault ADR 0024 is enabled, and none of them is
durable record:

- **Stale client registrations.** Registration is open by decision, so rows are
  unbounded. Pruned by **age plus liveness**, not by ``expires_at``: the SDK
  leaves ``client_secret_expiry_seconds`` unset and nothing else supplies one,
  so every row was NULL there and an expiry-only sweep deleted nothing at all
  while ``/register`` grew the table. A client with an unconsumed, unexpired
  refresh token is never a candidate whatever its age -- deleting one cascades
  to its tokens and would revoke a working connector. Neither is one with an
  authorization *in flight*: a pending authorization the operator is looking at,
  or a code minted seconds ago and not yet exchanged. Both cascade too, and an
  old registration reaches exactly that state when its refresh token expires and
  the connector reconnects -- the ordinary way back, not an edge case.
- **Expired pending authorizations.** A nonce nobody returned from, five minutes
  old.
- **Expired authorization codes.** Sixty seconds old, redeemed or not.
- **Expired refresh tokens.** Note the predicate is *expiry*, never consumption:
  a consumed token has to outlive its own rotation or replay detection stops
  working, since recognising it when it comes back is the whole mechanism.
- **Long-revoked OAuth credentials.** See below -- this one had a decision in it.

Usage:
    Local:      python -m scripts.prune_vault_oauth
    Dry run:    python -m scripts.prune_vault_oauth --dry-run
    Heroku:     heroku run --app <app> "python -m scripts.prune_vault_oauth"
    Scheduler:  python -m scripts.prune_vault_oauth

Environment variables:
    DATABASE_URL         Required. VAULT_DATABASE_URL takes precedence when set.

**Credentials are pruned by age, not by keeping the last N per identity**, and
the difference matters. Rotation mints a credential per refresh, so a
count-based rule looks natural -- keep the newest few, drop the rest. What it
cannot promise is that the rows it drops are dead ones: ordering by creation and
keeping N says nothing about whether the N+1st is still in use, and the symptom
of getting it wrong is a client mysteriously losing access.

Age has no such hazard. Thirty days matches the refresh token's own lifetime,
after which nothing in that chain can renew anyway, and only rows already
carrying ``revoked_at`` are ever considered -- an active credential is never a
candidate whatever its age.

Nothing is lost by removing them. The only foreign key onto
``vault_agent_credentials`` is ``vault_oauth_refresh_tokens.credential_id``
(CASCADE); audit events, write requests and ``contributed_by`` all carry text
correlation identifiers rather than references, deliberately (ADR 0002), so the
record of what a credential did outlives the credential itself.

**Operator-issued credentials are never touched.** The predicate requires the
``oauth-`` principal prefix, so a revoked ``importer`` or ``claude-1`` row stays
exactly where it is -- those are a census an operator reads, not machine
turnover.
"""

import argparse
import asyncio
import sys
from dataclasses import replace

from sqlalchemy import delete, func, select

from app.env import load_environment
from app.vault.db import create_vault_engine
from app.vault.oauth import PRINCIPAL_PREFIX
from app.vault.repository import (
    VaultOAuthAuthorizationCodeRepository,
    VaultOAuthClientRepository,
    VaultOAuthPendingAuthorizationRepository,
    VaultOAuthRefreshTokenRepository,
)
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_agent_credentials


# How long a revoked OAuth credential is kept. Matches REFRESH_TOKEN_TTL_SECONDS
# -- past it, nothing in that rotation chain could have renewed anyway -- and it
# is generous for the only reason to keep one at all, which is reading a recent
# incident. The durable record is the audit trail, which outlives this.
REVOKED_CREDENTIAL_RETENTION_DAYS = 30


def _transactions() -> tuple[VaultTransactionService, object]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    engine, observer = create_vault_engine(settings)
    return VaultTransactionService(engine, observer), engine


def _stale_oauth_credentials(retention_days: int):
    """Revoked OAuth credentials older than the retention window.

    Three predicates, and each is load-bearing:

    - ``principal_id LIKE 'oauth-%'`` keeps operator-issued credentials out of
      it entirely.
    - ``revoked_at IS NOT NULL`` means an active credential is never a
      candidate, whatever its age or how many newer ones share its principal.
    - the age window is what replaces a count, for the shared-principal reason
      in the module docstring.
    """

    return (
        vault_agent_credentials.c.principal_id.like(f"{PRINCIPAL_PREFIX}%"),
        vault_agent_credentials.c.revoked_at.is_not(None),
        vault_agent_credentials.c.revoked_at
        < func.now() - func.make_interval(0, 0, 0, retention_days),
    )


async def prune(dry_run: bool, retention_days: int) -> int:
    transactions, engine = _transactions()
    counts: dict[str, int] = {}
    try:
        async with transactions.transaction() as connection:
            if dry_run:
                # Count what would go, in one transaction that then rolls back
                # nothing because it wrote nothing. Cheaper and less alarming
                # than deleting and rolling back.
                counts["pending authorizations"] = await _count_expired(
                    connection, "vault_oauth_pending_authorizations"
                )
                counts["authorization codes"] = await _count_expired(
                    connection, "vault_oauth_authorization_codes"
                )
                counts["refresh tokens"] = await _count_expired(
                    connection, "vault_oauth_refresh_tokens"
                )
                counts["stale client registrations"] = (
                    await VaultOAuthClientRepository().count_stale(
                        connection, retention_days
                    )
                )
                result = await connection.execute(
                    select(func.count())
                    .select_from(vault_agent_credentials)
                    .where(*_stale_oauth_credentials(retention_days))
                )
                counts["revoked oauth credentials"] = int(result.scalar_one())
            else:
                # Order matters only for readability -- the cascades would
                # handle it -- but deleting the leaves first keeps the counts
                # meaning what they say rather than counting rows a cascade
                # already took.
                counts["pending authorizations"] = (
                    await VaultOAuthPendingAuthorizationRepository().delete_expired(
                        connection
                    )
                )
                counts["authorization codes"] = (
                    await VaultOAuthAuthorizationCodeRepository().delete_expired(
                        connection
                    )
                )
                counts["refresh tokens"] = (
                    await VaultOAuthRefreshTokenRepository().delete_expired(
                        connection
                    )
                )
                counts["stale client registrations"] = (
                    await VaultOAuthClientRepository().delete_stale(
                        connection, retention_days
                    )
                )
                removed = await connection.execute(
                    delete(vault_agent_credentials).where(
                        *_stale_oauth_credentials(retention_days)
                    )
                )
                counts["revoked oauth credentials"] = removed.rowcount or 0
    finally:
        await engine.dispose()

    verb = "would remove" if dry_run else "removed"
    for label, count in counts.items():
        print(f"{verb:<12} {count:>6}  {label}")
    print(
        f"\nRevoked OAuth credentials and idle registrations are kept for "
        f"{retention_days} days. Operator-issued credentials are never pruned, "
        "and a registration is never stale while it has a live refresh token "
        "or an authorization in flight."
    )
    return 0


async def _count_expired(connection, table_name: str) -> int:
    from app.vault.tables import metadata

    table = metadata.tables[f"vault.{table_name}"]
    result = await connection.execute(
        select(func.count())
        .select_from(table)
        .where(table.c.expires_at.is_not(None))
        .where(table.c.expires_at <= func.now())
    )
    return int(result.scalar_one())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune expired vault OAuth state and long-revoked OAuth credentials.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without removing it.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=REVOKED_CREDENTIAL_RETENTION_DAYS,
        help=(
            "How long a revoked OAuth credential is kept "
            f"(default {REVOKED_CREDENTIAL_RETENTION_DAYS})."
        ),
    )
    arguments = parser.parse_args()
    if arguments.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    load_environment()

    coroutine = prune(arguments.dry_run, arguments.retention_days)

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Same guard
    # as issue_vault_credential.py; a no-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
