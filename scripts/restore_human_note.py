"""
Restores a deleted Human note from its revision history (vault ADR 0052).

A Human deletion removes the row and keeps the history. This puts the row back,
under its original id, from the snapshot taken as it was deleted, so every client
that knew the note knows it again. Its revisions continue past the tombstone, and
it keeps its first author and the day it was first written.

An operator action rather than a route, on purpose: undoing a person's deletion is
rare, and a scope for it would be one more grant to issue carefully for no client
that needs it.

Refused when the note is still live, when no deletion of it is recorded, and when
another note has taken its path. In that last case move the other note first: a
restore never chooses a path of its own.

Usage:
    Dry run:   python -m scripts.restore_human_note --id <note-id>
    Restore:   python -m scripts.restore_human_note --id <note-id> --apply
    Heroku:    heroku run --app <app> "python -m scripts.restore_human_note --id <note-id> --apply"

Environment variables:
    DATABASE_URL         Required. VAULT_DATABASE_URL takes precedence when set.
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from uuid import uuid4

from app.env import load_environment
from app.vault.db import create_vault_engine, describe_database
from app.vault.repository import VaultDocumentRepository, VaultHumanHistoryRepository
from app.vault.service import (
    DocumentNotFound,
    HumanNoteNotDeleted,
    HumanPathTaken,
    HumanRestoreRequest,
    VaultHumanNoteService,
    VaultTransactionService,
)
from app.vault.settings import VaultSettings


# Who a restore is attributed to, in the history and the audit trail: the name
# the operator entitlement commands already record.
OPERATOR_PRINCIPAL = "operator-cli"


def _transactions() -> tuple[VaultTransactionService, object]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    # This script writes a row. It names the database before it does.
    print(f"database      : {describe_database(settings.database_url)}")
    engine, observer = create_vault_engine(settings)
    return VaultTransactionService(engine, observer), engine


async def restore(note_id: str, apply: bool) -> int:
    transactions, engine = _transactions()
    try:
        if not apply:
            return await _preview(transactions, note_id)
        try:
            outcome = await VaultHumanNoteService(transactions).restore(
                HumanRestoreRequest(
                    document_id=note_id,
                    principal_id=OPERATOR_PRINCIPAL,
                    request_id=uuid4().hex,
                )
            )
        except HumanNoteNotDeleted:
            print(f"{note_id} is not deleted; nothing to restore.", file=sys.stderr)
            return 1
        except DocumentNotFound:
            print(f"No deletion of {note_id} is recorded.", file=sys.stderr)
            return 1
        except HumanPathTaken as exc:
            print(
                f"Another note now has {exc}. Move that note first; a restore "
                "never chooses a path.",
                file=sys.stderr,
            )
            return 1
    finally:
        await engine.dispose()

    document = outcome.document
    print(f"restored      : {document.id}")
    print(f"path          : {document.vault_path}")
    print(f"revision      : {document.resource_revision}")
    return 0


async def _preview(transactions: VaultTransactionService, note_id: str) -> int:
    """What ``--apply`` would do, from the same reads, writing nothing.

    Exits 1 when the restore would be refused, so a dry run in a script stops
    before the apply that would fail.
    """

    documents = VaultDocumentRepository()
    history = VaultHumanHistoryRepository()
    async with transactions.transaction() as connection:
        live = await documents.get_by_id(connection, note_id)
        latest = await history.latest_change_for(connection, note_id)
        snapshot = await history.revision(connection, note_id, latest=True)
        holder = (
            await documents.id_at_path_ignoring_case(connection, snapshot.vault_path)
            if snapshot is not None
            else None
        )

    if live is not None:
        print(f"{note_id} is not deleted; nothing to restore.", file=sys.stderr)
        return 1
    if latest is None or latest.change_kind != "delete" or snapshot is None:
        print(f"No deletion of {note_id} is recorded.", file=sys.stderr)
        return 1

    print(f"would restore : {note_id}")
    print(f"title         : {snapshot.title}")
    print(f"path          : {snapshot.vault_path}")
    print(f"deleted at    : {latest.occurred_at.isoformat(timespec='seconds')}")
    print(f"new revision  : {snapshot.resource_revision + 1}")
    if holder is not None:
        print(
            f"\nRefused: {holder} now has that path. Move it first.",
            file=sys.stderr,
        )
        return 1
    print("\nDry run. Re-run with --apply to restore.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a deleted Human note from its revision history.",
    )
    parser.add_argument("--id", required=True, help="The deleted note's id.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Restore the note. Without it, report what would be restored.",
    )
    arguments = parser.parse_args()
    load_environment()

    coroutine = restore(arguments.id, arguments.apply)

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Same guard
    # as prune_vault_oauth.py; a no-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
