"""
Repoints ``source_ids`` and ``related_ids`` at the note ids the rows actually have.

A note's id is assigned by whichever import created it, so a corpus that has
been imported more than once has more than one id per logical note. References
written against an earlier generation keep pointing at ids nothing answers to.
Production is in that state today: all 49 ``source_ids`` across the fourteen
wiki pages name Stage-A ids, because ``import_vault_wiki`` carried the
frontmatter through verbatim while the notes had already been re-imported under
new ids. ``check-wiki`` reports each one as ``wiki-source-missing``.

**The maps are the evidence, and they chain.** Each ``import-map.json`` records
``{upstream_id: {note_id: ...}}`` for one import, which says only that those two
ids denote the same note. Feed every generation's map in and the pairs compose
into one equivalence class per note -- so a reference written against the
*pre-wipe* service ids resolves in two hops, upstream id first, without the
caller working out the order. Maps may be passed in any order for that reason.

A class must contain exactly one live id. Zero means the note is genuinely gone
and its references are left alone and reported; more than one means the same
upstream note exists twice in this database, which this script will not guess
its way through.

Nothing is rewritten in place that was already right: a reference naming a live
id is left exactly as it is, so a second run writes nothing.

Usage:
    Dry run:  python -m scripts.remap_vault_reference_ids --map ../knowledge-platform/import-map.json
    Write:    python -m scripts.remap_vault_reference_ids --map <path> [--map <older>] --apply

Environment variables:
    DATABASE_URL         Required. VAULT_DATABASE_URL takes precedence when set.
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import select, update

from app.env import load_environment
from app.vault.db import create_vault_engine, describe_database
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_documents


# The reference columns this repoints. Both are plain id arrays on
# `vault_documents`; `source_ids` is the one `check-wiki` gates on, and
# `related_ids` is unvalidated by the write path (ADR 0025) which is precisely
# why nothing else would ever notice it had gone stale.
REFERENCE_COLUMNS = ("source_ids", "related_ids")
_NOTE_ID = re.compile(r"^[0-9a-f]{32}$")


class IdentityClasses:
    """Every id a note has been known by, unioned across import generations."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def _find(self, item: str) -> str:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self._find(left), self._find(right)
        if left_root != right_root:
            self._parent[left_root] = right_root

    def members(self) -> dict[str, set[str]]:
        groups: dict[str, set[str]] = {}
        for item in list(self._parent):
            groups.setdefault(self._find(item), set()).add(item)
        return groups


@dataclass(frozen=True)
class Resolution:
    """How every known id maps onto a live one, and what could not be mapped."""

    canonical: dict[str, str]
    orphaned: dict[str, set[str]]
    ambiguous: dict[str, set[str]]


def load_maps(paths: list[Path]) -> IdentityClasses:
    classes = IdentityClasses()
    for path in paths:
        entries = json.loads(path.read_text(encoding="utf-8"))
        for upstream_id, record in entries.items():
            note_id = record.get("note_id") if isinstance(record, dict) else record
            if not note_id:
                continue
            classes.union(str(upstream_id), str(note_id))
        print(f"map        : {path} ({len(entries)} entries)")
    return classes


def resolve(classes: IdentityClasses, live_ids: set[str]) -> Resolution:
    canonical: dict[str, str] = {}
    orphaned: dict[str, set[str]] = {}
    ambiguous: dict[str, set[str]] = {}
    for root, group in classes.members().items():
        live = group & live_ids
        if len(live) == 1:
            target = next(iter(live))
            for alias in group:
                canonical[alias] = target
        elif not live:
            orphaned[root] = group
        else:
            ambiguous[root] = live
    return Resolution(canonical=canonical, orphaned=orphaned, ambiguous=ambiguous)


def remap(values: list[str], canonical: dict[str, str]) -> list[str]:
    """Repoint each reference, leaving anything unresolvable exactly as it is.

    Idempotent, which is what makes a second run write nothing: ``resolve`` maps
    every member of a class to the live one *including the live one itself*, so
    a reference that already names it maps to itself.

    Order is preserved, and so are duplicates. This repoints references, it does
    not tidy them, and a reader of the diff should see one kind of change.
    """
    return [canonical.get(value, value) for value in values]


@dataclass(frozen=True)
class Change:
    document_id: str
    vault_path: str
    column: str
    before: list[str]
    after: list[str]


async def run(map_paths: list[Path], apply: bool) -> int:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    # This script rewrites reference columns. It names the database first.
    print(f"database   : {describe_database(settings.database_url)}")
    classes = load_maps(map_paths)

    engine, observer = create_vault_engine(settings)
    transactions = VaultTransactionService(engine, observer)
    changes: list[Change] = []
    unresolved: dict[str, list[str]] = {}
    try:
        async with transactions.transaction() as connection:
            rows = (
                await connection.execute(
                    select(
                        vault_documents.c.id,
                        vault_documents.c.vault_path,
                        vault_documents.c.source_ids,
                        vault_documents.c.related_ids,
                    )
                )
            ).all()
            live_ids = {row.id for row in rows}
            resolution = resolve(classes, live_ids)

            for row in rows:
                for column in REFERENCE_COLUMNS:
                    before = list(getattr(row, column) or ())
                    after = remap(before, resolution.canonical)
                    if after != before:
                        changes.append(
                            Change(row.id, row.vault_path, column, before, after)
                        )
                    for value in after:
                        # Historical wiki `Related` values are Obsidian links,
                        # stored in this column before the service owned wiki
                        # pages. They are not document ids and must not be
                        # reported as broken ones. Every service and Stage-A
                        # note id is a lowercase UUID hex string.
                        if _NOTE_ID.fullmatch(value) and value not in live_ids:
                            unresolved.setdefault(value, []).append(row.vault_path)

            if apply:
                for change in changes:
                    await connection.execute(
                        update(vault_documents)
                        .where(vault_documents.c.id == change.document_id)
                        .values(**{change.column: change.after})
                    )
    finally:
        await engine.dispose()

    _report(changes, unresolved, resolution, applied=apply)
    return 0


def _report(
    changes: list[Change],
    unresolved: dict[str, list[str]],
    resolution: Resolution,
    applied: bool,
) -> None:
    verb = "rewrote" if applied else "would rewrite"
    by_column = {
        column: [c for c in changes if c.column == column] for column in REFERENCE_COLUMNS
    }
    print()
    for column, group in by_column.items():
        moved = sum(
            sum(1 for b, a in zip(c.before, c.after, strict=True) if b != a)
            for c in group
        )
        print(f"{verb:<14} {column:<12}: {moved} reference(s) across {len(group)} row(s)")

    for change in changes:
        print(f"\n  {change.vault_path}  [{change.column}]")
        for before, after in zip(change.before, change.after, strict=True):
            if before != after:
                print(f"    {before} -> {after}")

    if resolution.ambiguous:
        print(f"\n{len(resolution.ambiguous)} upstream note(s) map to more than one live id:")
        for root, live in resolution.ambiguous.items():
            print(f"  {root}: {', '.join(sorted(live))}")

    if unresolved:
        # Left as they are rather than dropped. A reference nothing answers to
        # is a fact about the corpus; silently deleting it would destroy the
        # only evidence that the note was ever cited.
        print(f"\n{len(unresolved)} reference(s) still resolve to nothing, and are left alone:")
        for value, paths in sorted(unresolved.items()):
            print(f"  {value}  cited by {', '.join(sorted(set(paths)))}")

    if not applied:
        print("\nDry run. Re-run with --apply to write.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repoint vault reference ids at the notes that currently exist.",
    )
    parser.add_argument(
        "--map",
        dest="maps",
        required=True,
        action="append",
        type=Path,
        help="An import-map.json. Repeat for each import generation; order "
        "does not matter, because the pairs compose.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the rewritten references. Without it nothing is changed.",
    )
    arguments = parser.parse_args()

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py and the other scripts here. No-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.maps, arguments.apply),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.maps, arguments.apply))


if __name__ == "__main__":
    sys.exit(main())
