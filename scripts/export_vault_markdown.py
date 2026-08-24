"""
Projects the service-authoritative ``Agent/`` tree out as markdown files.

ADR 0022: ``Agent/`` is authoritative in the database and reaches a human by
export; ``Human/`` is authoritative in markdown and reaches the service by
import. This is the export half. It is a projection, not a sync: it reads the
corpus and writes files, and nothing it writes is ever read back.

Dry run is the default, matching the other scripts here that touch a live
target. ``--apply`` writes; ``--apply --prune`` also deletes markdown files
under the exported folders that no row accounts for -- which is how a retired
note (ADR 0019 deletes rather than tombstones) actually leaves the tree.

The output directory is required and never defaults. Until the Stage-A write
path is retired, the live ``Vault/`` in the knowledge-platform repository still
has a second writer in it, and pointing this at that tree would mix the two.

Usage:
    Dry run:            python -m scripts.export_vault_markdown --out ../out
    Write:              python -m scripts.export_vault_markdown --out ../out --apply
    Write and prune:    python -m scripts.export_vault_markdown --out ../out --apply --prune

Environment variables:
    DATABASE_URL              Required. Vault schema must already be migrated.
    VAULT_DATABASE_URL        Optional override, as elsewhere in the vault.

No embedding provider is needed: this reads stored rows and touches neither the
vector arm nor the write path.
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

from app.env import load_environment
from app.vault.db import create_vault_engine, describe_database
from app.vault.export import EXPORTED_PATH_PREFIXES, ExportReport, VaultExportService
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings


def print_report(report: ExportReport, applied: bool, pruned: bool) -> None:
    verb = "wrote" if applied else "would write"
    print(f"\nscanned    : {report.scanned}")
    print(f"{verb:<11}: {report.written}")
    print(f"unchanged  : {report.unchanged}")

    if report.dropped:
        print("\nFields with no place in the Metadata Standard, so not exported:")
        for name, count in sorted(report.dropped.items()):
            print(f"  {name}: {count} document(s)")

    if report.warnings:
        print(f"\n{len(report.warnings)} document(s) need attention:")
        for warning in report.warnings:
            print(f"  {warning}")

    if report.prunable:
        action = f"deleted {report.pruned}" if pruned else "would delete"
        print(f"\n{len(report.prunable)} orphaned file(s) -- {action}:")
        for path in report.prunable:
            print(f"  {path}")
    elif pruned:
        print("\nNothing to prune.")

    if not applied:
        print("\nDry run. Re-run with --apply to write.")


async def run(root: Path, apply: bool, prune: bool) -> int:
    vault_settings = replace(VaultSettings.from_environment(), enabled=True)
    print(f"database   : {describe_database(vault_settings.database_url)}")
    print(f"output     : {root.resolve()}")
    print(f"prefixes   : {', '.join(EXPORTED_PATH_PREFIXES)}")

    engine, observer = create_vault_engine(vault_settings)
    try:
        service = VaultExportService(VaultTransactionService(engine, observer))
        report = await service.export(root, apply=apply, prune=prune)
    finally:
        await engine.dispose()

    print_report(report, applied=apply, pruned=prune)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the vault's Agent tree as markdown files.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Directory to project into. The vault root, so files land under "
        "<out>/Agent/notes/ and friends.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write files. Without it nothing on disk is touched.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Also delete markdown files under the exported folders that no "
        "document accounts for. Requires --apply to take effect; without it "
        "the candidates are only listed.",
    )
    arguments = parser.parse_args()

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. run_dev.py
    # and conftest.py handle this the same way. No-op on Linux/Heroku, where
    # SelectorEventLoop is already the default.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.out, arguments.apply, arguments.prune),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.out, arguments.apply, arguments.prune))


if __name__ == "__main__":
    sys.exit(main())
