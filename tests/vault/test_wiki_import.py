"""The Stage-A wiki importer's frontmatter parser and run grouping.

No database and no network. What the script does with rows was verified by
running it against the test database and diffing the re-export byte for byte;
what is worth pinning here is the part that would fail *silently* -- a parser
that guessed at a shape it did not understand, and the run grouping that decides
how much of the compile history survives.

The parser is the inverse of ``export.render_frontmatter`` and only of that. A
round-trip test against the renderer is therefore the strongest available
assertion: whatever the exporter can write, this must read back unchanged.
"""

from datetime import UTC, datetime

import pytest

from app.vault.export import NOTE_KEY_ORDER, WIKI_KEY_ORDER, dump_note
from scripts.import_vault_wiki import (
    FrontmatterError,
    ReferenceResolutionError,
    WikiPageFile,
    parse_note,
    resolve_page_source_ids,
)
from scripts.remap_vault_reference_ids import IdentityClasses, resolve


CANONICAL = """---
Type: Wiki Page
Status: Current
CreatedAt: 2026-08-13T21:52:47Z
LastUpdated: 2026-08-13T21:52:47Z
tags:
  - idempotency
  - api-design
Title: Idempotency and Identity in a Write Path
Summary: A one-line summary.
SourceIDs:
  - cb6a42ec77334a0392a8cbcce502c471
  - f66cd89c270d4eb19a0ae0a07a6390bc
CompiledBy: agent:librarian
CompiledAt: 2026-08-13T21:52:47Z
CompileRunID: run_20260813_215205
SchemaVersion: 1
Related:
  - "[[Tools That Write and Commit Files]]"
---
The body.
"""


def test_the_canonical_shape_parses() -> None:
    metadata, body = parse_note(CANONICAL)

    assert metadata["Type"] == "Wiki Page"
    assert metadata["tags"] == ["idempotency", "api-design"]
    assert metadata["SourceIDs"] == [
        "cb6a42ec77334a0392a8cbcce502c471",
        "f66cd89c270d4eb19a0ae0a07a6390bc",
    ]
    assert metadata["SchemaVersion"] == 1
    assert body == "The body.\n"


def test_a_quoted_value_loses_its_quotes_not_its_content() -> None:
    """Wikilinks are quoted because ``[`` opens a flow sequence in YAML."""

    metadata, _ = parse_note(CANONICAL)

    assert metadata["Related"] == ["[[Tools That Write and Commit Files]]"]


def test_an_empty_scalar_is_none_and_an_empty_list_is_a_list() -> None:
    """``Status:`` and ``tags: []`` render differently and must read back so."""

    metadata, _ = parse_note(
        "---\nStatus:\ntags: []\nTitle: T\n---\nbody\n"
    )

    assert metadata["Status"] is None
    assert metadata["tags"] == []


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all\n",
        "---\nTitle: T\nunterminated\n",
        "---\n  - orphan list item\n---\nbody\n",
        "---\nnot a key value line\n---\nbody\n",
    ],
)
def test_a_shape_it_does_not_understand_raises(text: str) -> None:
    """Refusing beats guessing.

    These files are machine-written, so anything outside the canonical shape
    means the assumption behind the whole script is wrong. Guessing would import
    corrupted metadata and say nothing.
    """

    with pytest.raises(FrontmatterError):
        parse_note(text)


def test_it_round_trips_whatever_the_exporter_writes() -> None:
    """The strongest available assertion, and the one that survives a change.

    The parser exists to read files the canonical renderer produced. Pinning it
    against that renderer rather than against a fixture means a future change to
    key order or quoting is caught here rather than at the next import.
    """

    metadata = {
        "Type": "Wiki Page",
        "Status": "Current",
        "CreatedAt": "2026-08-13T21:52:47Z",
        "LastUpdated": "2026-08-13T21:52:47Z",
        "tags": ["a", "b"],
        "Title": "A title: with a colon",
        "Summary": "Summary text.",
        "SourceIDs": ["abc", "def"],
        "CompiledBy": "agent:librarian",
        "CompiledAt": "2026-08-13T21:52:47Z",
        "CompileRunID": "run_20260813_215205",
        "SchemaVersion": 1,
        "Related": ["[[Some Page]]"],
    }
    rendered = dump_note(metadata, "The body.\n", WIKI_KEY_ORDER)

    parsed, body = parse_note(rendered)

    assert parsed == metadata
    assert body == "The body.\n"


def test_it_round_trips_a_note_shape_too() -> None:
    """Not used by the importer, but the same renderer writes both.

    A divergence here would mean the parser had quietly become wiki-specific,
    which is worth knowing before somebody reaches for it to import notes.
    """

    metadata = {
        "Type": "Agent Note",
        "Status": "Active",
        "CreatedAt": "2026-08-12T21:06:40Z",
        "LastUpdated": "2026-08-13T01:26:34Z",
        "tags": [],
        "Title": "A note",
        "ID": "01660d33ff4c43f49b59a2f4e2e4e80b",
        "ContributedBy": "agent:claude-code",
        "RelatedIDs": [],
        "SchemaVersion": 2,
    }
    rendered = dump_note(metadata, "Body.\n", NOTE_KEY_ORDER)

    parsed, _ = parse_note(rendered)

    assert parsed == metadata


def _page(run_id: str, compiled_at: str) -> WikiPageFile:
    from pathlib import Path

    return WikiPageFile(
        path=Path(f"{run_id}.md"),
        slug=run_id,
        metadata={
            "Title": "T",
            "CompiledAt": compiled_at,
            "CompileRunID": run_id,
            "CompiledBy": "agent:librarian",
        },
        body="body",
    )


def test_a_page_reports_its_run_and_its_compiled_instant() -> None:
    """Grouping keys, and the reason the import preserves four runs not one.

    Collapsing them into a single synthetic run would make provenance claim the
    whole wiki was compiled at once, which is not what happened.
    """

    page = _page("run_20260813_184935", "2026-08-13T18:56:45Z")

    assert page.run_key == "run_20260813_184935"
    assert page.compiled_at == datetime(2026, 8, 13, 18, 56, 45, tzinfo=UTC)
    assert page.compiled_by == "agent:librarian"


def test_source_ids_are_repointed_at_the_live_note_before_import() -> None:
    stage_a = "stage-a-note"
    live = "live-note"
    classes = IdentityClasses()
    classes.union(stage_a, live)
    page = _page("run", "2026-08-13T18:56:45Z")
    page.metadata["SourceIDs"] = [stage_a]

    [resolved_page] = resolve_page_source_ids(
        [page], resolve(classes, {live}), {live}
    )

    assert resolved_page.metadata["SourceIDs"] == [live]
    assert page.metadata["SourceIDs"] == [stage_a]


def test_an_unresolved_source_refuses_the_import() -> None:
    page = _page("run", "2026-08-13T18:56:45Z")
    page.metadata["SourceIDs"] = ["missing-note"]

    with pytest.raises(ReferenceResolutionError, match="missing-note"):
        resolve_page_source_ids([page], resolve(IdentityClasses(), set()), set())


def test_an_ambiguous_source_map_refuses_the_import() -> None:
    classes = IdentityClasses()
    classes.union("stage-a-note", "live-one")
    classes.union("stage-a-note", "live-two")
    page = _page("run", "2026-08-13T18:56:45Z")
    page.metadata["SourceIDs"] = ["stage-a-note"]

    with pytest.raises(ReferenceResolutionError, match="multiple live ids"):
        resolve_page_source_ids(
            [page],
            resolve(classes, {"live-one", "live-two"}),
            {"live-one", "live-two"},
        )
