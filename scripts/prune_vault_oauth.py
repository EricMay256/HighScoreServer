"""
Prunes the OAuth authorization server's transient state.

Four things accumulate once vault ADR 0024 is enabled, and none of them is
durable record:

- **Expired client registrations.** Registration is open by decision, so rows
  are unbounded. Removing one cascades to any pending authorization, unredeemed
  code, or refresh token belonging to it -- correct, since none can complete
  without its client.
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
count-based rule looks natural -- keep the newest few, drop the rest. It has a
failure mode that looks like the script working correctly: an OAuth principal is
``oauth-<slug(client_name)>``, so **two separate registrations that both call
themselves "Claude" share a principal**. Keep-newest-N ordered by creation would
then delete the older registration's *live* credential, and the symptom is one
client mysteriously losing access.

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
                counts["client registrations"] = await _count_expired(
                    connection, "vault_oauth_clients"
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
                counts["client registrations"] = (
                    await VaultOAuthClientRepository().delete_expired(connection)
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
        f"\nRevoked OAuth credentials are kept for {retention_days} days. "
        "Operator-issued credentials are never pruned."
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
