"""
Resolves ``[[Wikilink]]`` values stored in ``related_ids`` to the ids they name.

ADR 0025 settles that inside the database every edge is an id, with wikilinks
translated only at the import and export boundaries. Production does not match
that: ``import_vault_wiki`` carried each page's ``Related`` frontmatter through
verbatim, so twenty-one ``[[Title]]`` strings sit in ``related_ids`` where ids
belong. Nothing rejected them -- ``related_ids`` is deliberately not
existence-checked (ADR 0025), ``remap_vault_reference_ids`` deliberately excludes
them from its dangling-reference report, and the exporter wrote them back out
verbatim, so the bad data survived a full round trip looking correct.

**A name resolves by title, alias, or slug, and never by guess.** Two documents
may legitimately share a title, so a link naming more than one is reported and
left exactly as it is rather than pointed at whichever row sorted first.

**An unresolvable link is dropped, and the original is preserved first.** ADR
0025 drops an unresolved name on the grounds that "nothing is lost, because the
unresolved link is still in ``frontmatter`` exactly as written" -- which was not
true of these rows, whose ``frontmatter`` is empty. So this writes the original
list into ``frontmatter`` under the governance key for the row's kind before
rewriting the column, making the ADR's premise true rather than assuming it.
That key is an assigned key in ``export``, so the copy is evidence in the
database and is never re-emitted into the file.

Nothing is rewritten that was already right: a value that is already an id is
left exactly as it is, so a second run writes nothing.

**Run this before the next export, not after.** The exporter renders
``related_ids`` as wikilinks and omits every value that does not resolve to a
document, which a stored ``[[Title]]`` string does not -- so exporting first
empties the ``Related`` block of all thirteen pages. It says so rather than
doing it quietly (``export._warnings`` names each row), but the ordering is the
fix and the warning is only the alarm.

Usage:
    Dry run:  python -m scripts.resolve_vault_wikilinks
    Write:    python -m scripts.resolve_vault_wikilinks --apply

Environment variables:
    DATABASE_URL         Required. VAULT_DATABASE_URL takes precedence when set.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select, update

from app.env import load_environment
from app.vault.db import create_vault_engine, describe_database
from app.vault.domain import DocumentKind
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_documents
from app.vault.wikilinks import (
    EdgeResolution,
    LinkIndex,
    LinkTarget,
    resolve_edges,
    slug_of,
)


# Where a row's original ``related_ids`` is preserved when this rewrites it.
# The governance key the row's own file uses, so the copy reads as the
# frontmatter it came from rather than as a column name: `Related` on a Wiki
# Page, `RelatedIDs` on an Agent Note. Both are assigned keys in
# `export._ASSIGNED_KEYS`, so neither is projected back into the markdown.
ORIGINAL_FRONTMATTER_KEY = {
    DocumentKind.WIKI.value: "Related",
    DocumentKind.NOTE.value: "RelatedIDs",
}


@dataclass(frozen=True)
class Change:
    """One row's rewritten edge list, and the frontmatter copy that precedes it."""

    document_id: str
    vault_path: str
    resolution: EdgeResolution
    before: tuple[str, ...]
    # The key the original list is preserved under, and the whole frontmatter
    # value that writes. Both None when the row's frontmatter already holds a
    # copy under that key: the existing one is the older evidence and wins.
    preserve_key: str | None = None
    frontmatter: dict[str, Any] | None = None
    # The row's frontmatter as the plan read it, so a write that touches
    # frontmatter can name what it expected to find.
    before_frontmatter: dict[str, Any] = field(default_factory=dict)
    # Why this row is not safe to rewrite, or None. A conflicted row is
    # reported and left exactly as it is: refusing costs a rerun, and the
    # alternative costs the only record that a link was ever there.
    conflict: str | None = None


def plan(rows: Sequence[Any], index: LinkIndex) -> list[Change]:
    """What each row's ``related_ids`` should become. No I/O.

    ``rows`` are ``vault_documents`` rows carrying at least ``id``, ``kind``,
    ``vault_path``, ``related_ids`` and ``frontmatter``.
    """

    changes: list[Change] = []
    for row in rows:
        before = tuple(row.related_ids or ())
        # `resolve_names` because this is the repair: a bare title or a stray
        # single bracket is the same corruption as a stored `[[Title]]`, and an
        # operator running this has asked for exactly that to be resolved. The
        # lookup still never guesses -- an ambiguous title is reported.
        resolution = resolve_edges(before, index, resolve_names=True)
        # Ambiguity alone produces no rewrite and is still worth a Change: a
        # link naming two documents is the one outcome that needs a human, and
        # a row that is only ambiguous would otherwise vanish from the report.
        if not (resolution.changed or resolution.ambiguous):
            continue
        existing = dict(row.frontmatter or {})
        key = ORIGINAL_FRONTMATTER_KEY.get(row.kind)
        held = existing.get(key) if key is not None else None
        # The preserved copy is only evidence if it is evidence of *this* list.
        # An existing key holding something else is a different list from a
        # different run, so it neither needs rewriting nor stands in for the one
        # about to lose a link. Refuse rather than choose which to keep.
        conflict: str | None = None
        if resolution.dropped and key is not None and key in existing:
            if held != list(before):
                conflict = (
                    f"frontmatter.{key} already holds a different list "
                    f"({held!r}), and rewriting would drop "
                    f"{', '.join(resolution.dropped)} with nothing preserving it"
                )
        preserve = (
            resolution.changed
            and key is not None
            and key not in existing
            and conflict is None
        )
        changes.append(
            Change(
                document_id=row.id,
                vault_path=row.vault_path,
                resolution=resolution,
                before=before,
                preserve_key=key if preserve else None,
                frontmatter=({**existing, key: list(before)} if preserve else None),
                before_frontmatter=existing,
                conflict=conflict,
            )
        )
    return changes


async def run(apply: bool) -> int:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    # This script rewrites reference columns. It names the database first.
    print(f"database   : {describe_database(settings.database_url)}")

    engine, observer = create_vault_engine(settings)
    transactions = VaultTransactionService(engine, observer)
    try:
        async with transactions.transaction() as connection:
            rows = (
                await connection.execute(
                    select(
                        vault_documents.c.id,
                        vault_documents.c.kind,
                        vault_documents.c.vault_path,
                        vault_documents.c.title,
                        vault_documents.c.aliases,
                        vault_documents.c.related_ids,
                        vault_documents.c.frontmatter,
                    )
                )
            ).all()
            # Every row is a link target, whatever its kind: `related_ids` does
            # not care which kind it points at, and a page citing a note is an
            # ordinary edge.
            index = LinkIndex(
                LinkTarget(
                    document_id=row.id,
                    title=row.title,
                    slug=slug_of(row.vault_path),
                    aliases=tuple(row.aliases or ()),
                )
                for row in rows
            )
            changes = plan(rows, index)
            stale: list[Change] = []

            if apply:
                for change in changes:
                    if not change.resolution.changed or change.conflict is not None:
                        continue
                    values: dict[str, object] = {
                        "related_ids": list(change.resolution.values)
                    }
                    if change.frontmatter is not None:
                        # Preserved in the same statement that rewrites the
                        # column, so a run that dies between the two cannot
                        # leave a dropped link with nowhere to have gone.
                        values["frontmatter"] = change.frontmatter
                    # The plan was built from a snapshot this transaction took
                    # at READ COMMITTED, and this script takes no corpus lock,
                    # so a governed update may have committed against this row
                    # in between. Naming the values the plan was built from
                    # makes the write refuse instead of reverting that update:
                    # zero rows means the row moved, and this repair is
                    # idempotent, so the answer is to rerun it rather than to
                    # hold the whole corpus still while it runs.
                    statement = (
                        update(vault_documents)
                        .where(vault_documents.c.id == change.document_id)
                        .where(vault_documents.c.related_ids == list(change.before))
                    )
                    if change.frontmatter is not None:
                        # Only when this write touches frontmatter: a governed
                        # update may have changed it without touching
                        # `related_ids`, and writing the planned dict would
                        # revert that too.
                        statement = statement.where(
                            vault_documents.c.frontmatter
                            == change.before_frontmatter
                        )
                    result = await connection.execute(statement.values(**values))
                    if result.rowcount != 1:
                        stale.append(change)
    finally:
        await engine.dispose()

    return _report(changes, stale, applied=apply)


def _report(changes: list[Change], stale: list[Change], applied: bool) -> int:
    verb = "rewrote" if applied else "would rewrite"
    refused = [change for change in changes if change.conflict is not None]
    writable = [change for change in changes if change.conflict is None]
    rewritten = [change for change in writable if change.resolution.changed]
    resolved = sum(len(c.resolution.resolved) for c in writable)
    dropped = sum(len(c.resolution.dropped) for c in writable)
    preserved = sum(1 for c in changes if c.preserve_key is not None)
    ambiguous = [(c, link, ids) for c in changes for link, ids in c.resolution.ambiguous]
    named = [
        (c, value)
        for c in changes
        for value in c.resolution.malformed
    ]

    print()
    print(f"{verb:<14} related_ids : {resolved} link(s) across {len(rewritten)} row(s)")
    for change in changes:
        print(f"\n  {change.vault_path}")
        for link, document_id in change.resolution.resolved:
            print(f"    {link} -> {document_id}")
        for link in change.resolution.dropped:
            print(f"    {link} -> dropped, names no document")
        if change.preserve_key is not None:
            print(f"    frontmatter.{change.preserve_key} <- the original list")

    if preserved:
        print(f"\n{preserved} original list(s) preserved in frontmatter before rewriting.")
    if dropped:
        print(
            f"\n{dropped} link(s) name no document and were dropped from related_ids. "
            "ADR 0025: an unresolved name is not an id."
        )

    if named:
        # Worth naming separately from the resolution: these are the values the
        # exporter warns about, and until this script grew `resolve_names` they
        # were the ones it could not actually repair.
        print(
            f"\n{len(named)} value(s) named a document without being a [[wikilink]], "
            "and were resolved as names:"
        )
        for change, value in named:
            print(f"  {change.vault_path}  {value!r}")

    if ambiguous:
        # Left in place, and the run still succeeds for everything else. A name
        # meaning two notes needs a human: the fix for a genuine duplicate and
        # the fix for two notes that happen to share a title are not the same.
        print(f"\n{len(ambiguous)} link(s) name more than one document, and are left alone:")
        for change, link, candidates in ambiguous:
            print(f"  {change.vault_path}  {link} -> {', '.join(candidates)}")

    if refused:
        print(f"\n{len(refused)} row(s) refused, and nothing was written to them:")
        for change in refused:
            print(f"  {change.vault_path}\n    {change.conflict}")

    if stale:
        print(
            f"\n{len(stale)} row(s) changed underneath this run and were left alone. "
            "Re-run: the repair is idempotent, so a second pass picks up exactly "
            "what this one skipped."
        )
        for change in stale:
            print(f"  {change.vault_path}")

    if not applied:
        print("\nDry run. Re-run with --apply to write.")
    # A refused or stale row is work this run was asked to do and did not do,
    # so the exit code says so. Ambiguity deliberately still exits 0: it is
    # pre-existing behaviour and whether it should signal is a separate
    # question from these two, which is being settled elsewhere.
    return 1 if (refused or stale) else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve wikilinks stored in vault related_ids to document ids.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the resolved references. Without it nothing is changed.",
    )
    arguments = parser.parse_args()

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py and the other scripts here. No-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.apply),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.apply))


if __name__ == "__main__":
    sys.exit(main())
