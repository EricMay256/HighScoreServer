"""The wiki import's under-lock revalidation.

Its whole job is to refuse, so the test that matters most is the one proving it
*doesn't* refuse an unchanged corpus. Its absence is precisely how a
revalidation that rejected every import passed CI: every existing test drove
planning or parsing, and none drove a successful apply through the new path.
"""

from pathlib import Path

import pytest

from scripts.import_vault_wiki import (
    ReferenceResolutionError,
    WikiPageFile,
    _revalidate_under_lock,
    plan_pages,
)


class _Row:
    """A `vault_documents` row as the revalidation reads it."""

    def __init__(self, id, vault_path, kind, title, aliases=()):
        self.id = id
        self.vault_path = vault_path
        self.kind = kind
        self.title = title
        self.aliases = tuple(aliases)


class _Connection:
    """Returns one fixed row set, which is the corpus under the lock."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        rows = self._rows

        class _Result:
            def all(self):
                return rows

        return _Result()


def _Page(slug, title, related=(), sources=()):
    """A real `WikiPageFile`, because `resolve_page_source_ids` calls
    `dataclasses.replace` on it."""

    return WikiPageFile(
        path=Path(f"{slug}.md"),
        slug=slug,
        metadata={
            "Title": title,
            "Related": list(related),
            "SourceIDs": list(sources),
        },
        body="Body.",
    )


def _plan(pages, rows):
    from app.vault.wikilinks import LinkTarget, slug_of

    targets = [
        LinkTarget(
            document_id=row.id,
            title=row.title,
            slug=slug_of(row.vault_path),
            aliases=row.aliases,
        )
        for row in rows
    ]
    return plan_pages(pages, targets)


class _NoMaps:
    """Stands in for `IdentityClasses` with no import maps loaded.

    `resolve` asks it for `members()`; an empty mapping means no id was ever
    aliased, which is the shape these fixtures want.
    """

    @staticmethod
    def members() -> dict[str, set[str]]:
        return {}


async def _revalidate(pages, planned, rows):
    await _revalidate_under_lock(
        _Connection(rows),
        pages=pages,
        planned_pages=planned,
        classes=_NoMaps(),
    )


def test_an_unchanged_corpus_passes_revalidation() -> None:
    """The case that was never exercised, and that always failed.

    `plan_pages` minted fresh ids on every call, so comparing plans keyed on
    those ids compared two disjoint sets: every page read as changed and no
    import could commit. The refusal looked exactly like the race it existed
    to catch.
    """

    rows = [_Row("note-1", "Agent/notes/a-source.md", "note", "A Source Note")]
    # Carrying real edges of both kinds -- one to a live note, one to a sibling
    # in this batch -- because a fixture with no edges compares empty tuples
    # and would pass whatever the ids did.
    pages = [
        _Page("one", "Page One", related=["[[A Source Note]]", "[[Page Two]]"]),
        _Page("two", "Page Two"),
    ]
    planned = _plan(pages, rows)
    assert planned[0].related_ids != (), "fixture must exercise edge resolution"

    # Must not raise.
    import asyncio

    asyncio.run(_revalidate(pages, planned, rows))


def test_sibling_links_keep_the_ids_the_first_plan_minted() -> None:
    """Re-minting would rewrite the very edges being compared, so a page
    linking to its sibling has to resolve to the same id both times."""

    rows: list[_Row] = []
    pages = [_Page("one", "Page One", related=["[[Page Two]]"]), _Page("two", "Page Two")]
    planned = _plan(pages, rows)

    two = next(p for p in planned if p.file.slug == "two")
    one = next(p for p in planned if p.file.slug == "one")
    assert one.related_ids == (two.document_id,)

    import asyncio

    asyncio.run(_revalidate(pages, planned, rows))


def test_a_newly_ambiguous_title_refuses_the_import() -> None:
    """A second document acquiring the title makes a unique link ambiguous
    without touching anything this import wrote -- the race the lock exists
    for."""

    pages = [_Page("one", "Page One", related=["[[A Source Note]]"])]
    before = [_Row("note-1", "Agent/notes/a-source.md", "note", "A Source Note")]
    planned = _plan(pages, before)

    after = before + [
        _Row("note-2", "Agent/notes/another.md", "note", "A Source Note")
    ]

    import asyncio

    with pytest.raises(ReferenceResolutionError, match="ambiguous"):
        asyncio.run(_revalidate(pages, planned, after))


def test_a_renamed_target_refuses_when_the_edge_moves() -> None:
    """The link resolved to a document that no longer answers to that name, so
    the edge would now be dropped rather than stored."""

    pages = [_Page("one", "Page One", related=["[[A Source Note]]"])]
    before = [_Row("note-1", "Agent/notes/a-source.md", "note", "A Source Note")]
    planned = _plan(pages, before)

    after = [_Row("note-1", "Agent/notes/a-source.md", "note", "Renamed Entirely")]

    import asyncio

    with pytest.raises(ReferenceResolutionError, match="changed while this import"):
        asyncio.run(_revalidate(pages, planned, after))


def test_a_claimed_page_path_refuses_the_import() -> None:
    """Another writer took a path this import is about to write."""

    pages = [_Page("one", "Page One")]
    before: list[_Row] = []
    planned = _plan(pages, before)

    after = [_Row("wiki-1", "Agent/wiki/one.md", "wiki", "Something Else")]

    import asyncio

    with pytest.raises(ReferenceResolutionError, match="claimed"):
        asyncio.run(_revalidate(pages, planned, after))
