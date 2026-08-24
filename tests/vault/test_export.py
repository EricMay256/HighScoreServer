"""The markdown projection of the service-authoritative ``Agent/`` tree.

Two levels. Rendering and path resolution are pure and tested directly. The
export loop is tested against an in-memory stand-in for the transaction and
repository, because what it has to get right — idempotency, pruning, refusing a
path — is filesystem behaviour rather than SQL. ``list_under_path_prefixes``
itself is exercised against the database at the bottom of this file.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from app.vault import export as export_module
from app.vault.db import create_vault_engine
from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    PromotionStatus,
    VaultDocument,
)
from app.vault.export import (
    CORPUS_OWNED_PATH_PREFIXES,
    EXPORTED_PATH_PREFIXES,
    ExportPathError,
    ExportReport,
    VaultExportService,
    render_document,
    render_wiki_index,
    resolve_export_path,
    utc_timestamp,
)
from app.vault.repository import VaultDocumentRepository
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings
from app.vault.tables import vault_documents


CREATED_AT = datetime(2026, 8, 12, 21, 6, 40, tzinfo=UTC)
UPDATED_AT = datetime(2026, 8, 13, 1, 26, 34, tzinfo=UTC)


def make_document(**overrides: object) -> VaultDocument:
    document = VaultDocument(
        id="01660d33ff4c43f49b59a2f4e2e4e80b",
        kind=DocumentKind.NOTE,
        status=DocumentStatus.ACTIVE,
        vault_path="Agent/notes/01660d33ff4c43f49b59a2f4e2e4e80b.md",
        title="A .env loaded from a checkout-relative path disappears",
        body="Two loaders, two answers.",
        contributed_by="agent:claude-code",
        provenance={"principal_id": "importer"},
        schema_version=1,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
        doc_type="Agent Note",
        doc_status="Active",
        tags=("gotcha", "git"),
    )
    return replace(document, **overrides)  # type: ignore[arg-type]


def test_note_renders_in_the_canonical_agent_note_shape() -> None:
    rendered = render_document(make_document())

    assert rendered.content == (
        "---\n"
        "Type: Agent Note\n"
        "Status: Active\n"
        "CreatedAt: 2026-08-12T21:06:40Z\n"
        "LastUpdated: 2026-08-13T01:26:34Z\n"
        "tags:\n"
        "  - gotcha\n"
        "  - git\n"
        "Title: A .env loaded from a checkout-relative path disappears\n"
        "ID: 01660d33ff4c43f49b59a2f4e2e4e80b\n"
        "ContributedBy: agent:claude-code\n"
        "Source:\n"
        "RelatedIDs: []\n"
        "ClientRunID:\n"
        "SchemaVersion: 2\n"
        "---\n"
        "Two loaders, two answers.\n"
    )


def test_timestamps_are_utc_whatever_the_session_timezone_was() -> None:
    """``timestamptz`` arrives in the session's zone, not UTC.

    Rendering it as-is would make the same row produce different bytes on two
    machines, which is the one thing an audit-log projection may not do. This
    is the unit-level statement of what a run under ``PGTZ=Asia/Tokyo``
    confirmed against the real corpus.
    """

    tokyo = timezone(timedelta(hours=9))
    shifted = make_document(created_at=CREATED_AT.astimezone(tokyo))

    assert utc_timestamp(CREATED_AT.astimezone(tokyo)) == "2026-08-12T21:06:40Z"
    assert render_document(shifted).content == render_document(
        make_document()
    ).content


def test_flagged_notes_are_exported_with_their_status_map_value() -> None:
    """ADR 0008 withholds flagged from agents; a librarian is the other reader.

    ``types.yml`` gives ``Agent Note`` exactly ``Active`` and ``Flagged``, so
    the projection has a legal value to write rather than an anomaly to hide.
    """

    rendered = render_document(
        make_document(status=DocumentStatus.FLAGGED, doc_status="Flagged")
    )

    assert "\nStatus: Flagged\n" in rendered.content
    assert rendered.warnings == ()


def test_a_title_that_looks_like_yaml_is_quoted() -> None:
    rendered = render_document(make_document(title="Load probe: pool saturation"))

    assert '\nTitle: "Load probe: pool saturation"\n' in rendered.content


def test_optional_keys_are_omitted_rather_than_rendered_empty() -> None:
    """An ordinary note keeps Stage A's exact key set.

    ``aliases``, ``Facets``, ``Summary``, and ``SourceIDs`` are additions this
    projection can make and the Stage-A note model could not. Rendering them
    empty on every note would put four keys into 53 files to describe nothing.
    """

    content = render_document(make_document()).content

    assert "aliases" not in content
    assert "Facets" not in content
    assert "Summary" not in content
    assert "SourceIDs" not in content


def test_populated_optional_keys_land_in_governance_order() -> None:
    rendered = render_document(
        make_document(
            aliases=("dotenv worktree",),
            facets={"project": ["hss"]},
            summary="Two loaders resolve .env differently.",
            source_ids=("abc123",),
            related_ids=("def456",),
            source_url="https://example.invalid/run/1",
        )
    )
    frontmatter = rendered.content.split("---\n")[1]
    keys = [
        line.split(":", 1)[0]
        for line in frontmatter.splitlines()
        if line and not line.startswith(" ")
    ]

    assert keys == [
        "Type",
        "Status",
        "CreatedAt",
        "LastUpdated",
        "tags",
        "aliases",
        "Facets",
        "Title",
        "Summary",
        "ID",
        "ContributedBy",
        "Source",
        "RelatedIDs",
        "SourceIDs",
        "ClientRunID",
        "SchemaVersion",
    ]


def test_unmodelled_frontmatter_is_re_emitted_but_never_shadows_a_column() -> None:
    """``frontmatter`` JSONB exists so the projection can re-emit what a note
    said. A key that also comes from a column is the same fact twice, and the
    column is the current one."""

    rendered = render_document(
        make_document(frontmatter={"ReviewFreq": "Monthly", "Title": "stale copy"})
    )

    assert "\nReviewFreq: Monthly\n" in rendered.content
    assert "stale copy" not in rendered.content


def test_facets_are_projected_as_flat_name_slash_value_entries() -> None:
    """The Metadata Standard's universal `Facets` property, per ADR 0017.

    Flat rather than nested: the canonical renderer emits scalars and block
    sequences only, and a value may itself contain slashes because the split is
    on the first one.
    """

    rendered = render_document(
        make_document(facets={"project": ["hss"], "system": ["postgres/pgvector"]})
    )

    assert rendered.dropped_fields == ()
    assert (
        "\nFacets:\n  - project/hss\n  - system/postgres/pgvector\n"
        in rendered.content
    )


def test_facet_entries_are_sorted_so_the_file_does_not_churn() -> None:
    unsorted = render_document(
        make_document(facets={"system": ["redis"], "area": ["vault"]})
    )
    reordered = render_document(
        make_document(facets={"area": ["vault"], "system": ["redis"]})
    )

    assert unsorted.content == reordered.content


def test_origin_supplies_the_governance_keys_it_has_answers_for() -> None:
    """`ContributedBy` and `CreatedAt` mean what the *note* says.

    For replayed content those are the upstream facts, not the vault's:
    `contributed_by` names the credential that transmitted it (ADR 0016) and
    `created_at` is when the row landed. Who transmitted and when stays in the
    write ledger and the audit events.
    """

    rendered = render_document(
        make_document(
            contributed_by="agent:importer",
            origin={
                "author": "agent:codex",
                "created_at": "2026-07-30T18:54:39Z",
                "updated_at": "2026-07-31T09:00:00Z",
                "reference": "HighScoreServer vault import session 2026-08-12",
                "run_id": "crlf-patch-transit-2026-07-29",
            },
        )
    )

    assert "\nContributedBy: agent:codex\n" in rendered.content
    assert "agent:importer" not in rendered.content
    assert "\nCreatedAt: 2026-07-30T18:54:39Z\n" in rendered.content
    assert "\nLastUpdated: 2026-07-31T09:00:00Z\n" in rendered.content
    assert (
        "\nSource: HighScoreServer vault import session 2026-08-12\n"
        in rendered.content
    )
    assert "\nClientRunID: crlf-patch-transit-2026-07-29\n" in rendered.content


def test_origin_timestamps_are_re_emitted_verbatim() -> None:
    """Stored as the ISO-8601 text the upstream note carried, so there is no
    parse-and-reformat step that could move a value or churn a file."""

    rendered = render_document(
        make_document(origin={"created_at": "2026-07-30T18:54:39.123456+00:00"})
    )

    assert "\nCreatedAt: 2026-07-30T18:54:39.123456+00:00\n" in rendered.content


def test_an_empty_origin_leaves_the_vault_as_the_author() -> None:
    """The ordinary case: an agent contributing now is author and contributor."""

    rendered = render_document(make_document(contributed_by="agent:claude-code"))

    assert "\nContributedBy: agent:claude-code\n" in rendered.content
    assert "\nClientRunID:\n" in rendered.content


def test_a_row_with_no_type_or_status_is_still_written_but_reported() -> None:
    rendered = render_document(make_document(doc_type=None, doc_status=None))

    assert len(rendered.warnings) == 2
    assert rendered.content.startswith("---\nType:\nStatus:\n")


def test_wiki_pages_render_with_compile_provenance() -> None:
    run_id = UUID("6f1d4c1a4b7f4a1e9c2d3e4f5a6b7c8d")
    rendered = render_document(
        make_document(
            kind=DocumentKind.WIKI,
            vault_path="Agent/wiki/idempotency.md",
            doc_type="Wiki Page",
            doc_status="Current",
            source_ids=("note-a", "note-b"),
            compile_run_id=run_id,
            compiled_by="agent:librarian",
            compiled_at=UPDATED_AT,
        )
    )

    assert "\nCompiledBy: agent:librarian\n" in rendered.content
    assert "\nCompiledAt: 2026-08-13T01:26:34Z\n" in rendered.content
    assert f"\nCompileRunID: {run_id}\n" in rendered.content
    # types.yml pins a Wiki Page to schema 1, an Agent Note to 2.
    assert "\nSchemaVersion: 1\n" in rendered.content
    assert "\nID:" not in rendered.content


@pytest.mark.parametrize(
    "vault_path",
    [
        # The AI Contribution Policy forbids agents the Human layer outright.
        "Human/06 Reference/Postgres.md",
        # No `Agent/**` catch-all exists, so an unclassified subfolder is not
        # projected either.
        "Agent/experiments/scratch.md",
    ],
)
def test_paths_outside_the_engine_managed_folders_are_refused(
    tmp_path: Path,
    vault_path: str,
) -> None:
    with pytest.raises(ExportPathError):
        resolve_export_path(tmp_path, vault_path)


def test_export_paths_stay_inside_the_output_directory(tmp_path: Path) -> None:
    resolved = resolve_export_path(tmp_path, "Agent/notes/abc.md")

    assert resolved == (tmp_path / "Agent" / "notes" / "abc.md").resolve()


def test_exported_prefixes_are_all_under_the_agent_tree() -> None:
    assert all(prefix.startswith("Agent/") for prefix in EXPORTED_PATH_PREFIXES)


class _StubTransactions:
    """Stands in for VaultTransactionService: the export loop never uses the
    connection for anything but handing it to the repository.

    Records the isolation level it was asked for, which is the one thing about
    the transaction the exporter genuinely depends on -- paging under READ
    COMMITTED sees a different corpus on every page.
    """

    def __init__(self) -> None:
        self.isolation_levels: list[str | None] = []

    @asynccontextmanager
    async def transaction(
        self, isolation_level: str | None = None
    ) -> AsyncIterator[None]:
        self.isolation_levels.append(isolation_level)
        yield None


class _StubDocuments:
    """Serves one page then stops, matching the repository's keyset contract."""

    def __init__(self, documents: tuple[VaultDocument, ...]) -> None:
        self._documents = tuple(
            sorted(documents, key=lambda document: document.vault_path)
        )

    async def list_under_path_prefixes(
        self,
        connection: object,
        prefixes: tuple[str, ...],
        after_vault_path: str | None = None,
        limit: int = 200,
    ) -> tuple[VaultDocument, ...]:
        remaining = [
            document
            for document in self._documents
            if after_vault_path is None or document.vault_path > after_vault_path
        ]
        return tuple(remaining[:limit])


def export_to(
    tmp_path: Path,
    documents: tuple[VaultDocument, ...],
    **kwargs: bool,
) -> ExportReport:
    service = VaultExportService(
        _StubTransactions(),  # type: ignore[arg-type]
        _StubDocuments(documents),  # type: ignore[arg-type]
        page_size=2,
    )
    return asyncio.run(service.export(tmp_path, **kwargs))


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    report = export_to(tmp_path, (make_document(),))

    assert report.scanned == 1
    assert report.written == 1
    assert not list(tmp_path.rglob("*.md"))


def test_writing_twice_produces_an_identical_tree(tmp_path: Path) -> None:
    documents = (
        make_document(),
        make_document(
            id="beefbeefbeefbeefbeefbeefbeefbeef",
            vault_path="Agent/notes/beefbeefbeefbeefbeefbeefbeefbeef.md",
        ),
        make_document(
            id="cafecafecafecafecafecafecafecafe",
            vault_path="Agent/notes/cafecafecafecafecafecafecafecafe.md",
        ),
    )

    first = export_to(tmp_path, documents, apply=True)
    before = {
        path: path.read_bytes() for path in sorted(tmp_path.rglob("*.md"))
    }
    second = export_to(tmp_path, documents, apply=True)
    after = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*.md"))}

    assert first.written == 3
    assert second.written == 0
    assert second.unchanged == 3
    assert before == after


def test_files_are_written_with_lf_endings(tmp_path: Path) -> None:
    """The knowledge-platform .gitattributes pins markdown to ``eol=lf``.

    Python's default text mode would write CRLF on Windows, which is how the
    Stage-A engine's files ended up mixed. Left alone it makes every run on a
    Windows machine rewrite every file.
    """

    export_to(tmp_path, (make_document(),), apply=True)
    written = next(tmp_path.rglob("*.md")).read_bytes()

    assert b"\r\n" not in written


def test_a_removed_document_leaves_an_orphan_that_prune_deletes(
    tmp_path: Path,
) -> None:
    retired = make_document(
        id="deaddeaddeaddeaddeaddeaddeaddead",
        vault_path="Agent/notes/deaddeaddeaddeaddeaddeaddeaddead.md",
    )
    export_to(tmp_path, (make_document(), retired), apply=True)

    listed = export_to(tmp_path, (make_document(),), apply=True)
    assert listed.prunable == [
        "Agent/notes/deaddeaddeaddeaddeaddeaddeaddead.md"
    ]
    assert listed.pruned == 0

    pruned = export_to(tmp_path, (make_document(),), apply=True, prune=True)
    assert pruned.pruned == 1
    assert not (tmp_path / retired.vault_path).exists()


def test_prune_never_reaches_outside_the_exported_prefixes(
    tmp_path: Path,
) -> None:
    """``Agent/INDEX.md`` sits under no exported prefix at all.

    Pruning walks the owned prefixes and nothing else, so a file sitting beside
    them -- however much it looks like an orphan -- is not this projection's to
    delete.

    This test used to make a second point with a Stage-A page under
    ``Agent/wiki/``: a prefix the export writes but does not own. That example
    retired on 2026-08-24, when the fourteen pages became rows and the prefix
    joined ``CORPUS_OWNED_PATH_PREFIXES``. The rule it showed is still live and
    is tested below against a prefix held out deliberately, rather than against
    whichever one happens to be unowned today.
    """

    (tmp_path / "Agent").mkdir()
    index = tmp_path / "Agent" / "INDEX.md"
    index.write_text("wiki index\n", encoding="utf-8")

    report = export_to(tmp_path, (make_document(),), apply=True, prune=True)

    assert report.prunable == []
    assert index.exists()

def test_list_under_path_prefixes_pages_the_agent_tree(
    configure_test_env: None,
) -> None:
    """The new repository query, against the database.

    Ordering by ``vault_path`` is what makes the keyset cursor total, and the
    prefix filter is what keeps ``Human/`` out of a projection that writes
    files.
    """

    async def exercise() -> None:
        settings = replace(VaultSettings.from_environment(), enabled=True)
        engine, observer = create_vault_engine(settings)
        transactions = VaultTransactionService(engine, observer)
        documents = VaultDocumentRepository()
        marker = uuid4().hex
        agent_ids = [f"zzexport-{marker}-{index}" for index in range(3)]
        human_id = f"zzexport-{marker}-human"

        try:
            async with transactions.transaction() as connection:
                for index, document_id in enumerate(agent_ids):
                    await documents.insert(
                        connection,
                        NewVaultDocument(
                            id=document_id,
                            kind=DocumentKind.NOTE,
                            vault_path=f"Agent/notes/{document_id}.md",
                            status=DocumentStatus.ACTIVE,
                            title=f"Export page {index}",
                            body="Paged by keyset on vault_path.",
                            contributed_by="test:export",
                            provenance={"fixture": True},
                        ),
                    )
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=human_id,
                        kind=DocumentKind.NOTE,
                        vault_path=f"Human/06 Reference/{human_id}.md",
                        status=DocumentStatus.ACTIVE,
                        title="Not projected",
                        body="Markdown is authoritative for this tree.",
                        contributed_by="test:export",
                        provenance={"fixture": True},
                    ),
                )

            async with transactions.transaction() as connection:
                first = await documents.list_under_path_prefixes(
                    connection,
                    EXPORTED_PATH_PREFIXES,
                    after_vault_path=f"Agent/notes/zzexport-{marker}-",
                    limit=2,
                )
                second = await documents.list_under_path_prefixes(
                    connection,
                    EXPORTED_PATH_PREFIXES,
                    after_vault_path=first[-1].vault_path,
                    limit=2,
                )
                everything = await documents.list_under_path_prefixes(
                    connection,
                    EXPORTED_PATH_PREFIXES,
                    limit=1000,
                )

            assert [document.id for document in first] == agent_ids[:2]
            # Only the first entry of the second page is ours: another test's
            # `Agent/wiki/` row sorts after `Agent/notes/` and may follow.
            assert second[0].id == agent_ids[2]
            assert human_id not in {
                document.id for document in (*first, *second, *everything)
            }
        finally:
            async with transactions.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.in_([*agent_ids, human_id])
                    )
                )
            await engine.dispose()

    asyncio.run(exercise())


def test_prune_leaves_a_prefix_the_corpus_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix the export *writes* but does not *own* is never swept.

    ADR 0023's distinction, and what stops one ``--apply --prune`` deleting a
    second writer's work: the file has no row, so an occupancy test would call
    it an orphan, and only the owned set says otherwise.

    Held out with a patched constant rather than by naming whichever prefix is
    unowned today. As of 2026-08-24 the owned set and the exported set are
    equal -- ``Agent/wiki/`` was the last hold-out and its pages are rows now --
    so a test written against the live values would assert nothing at all while
    still appearing to pass.
    """

    monkeypatch.setattr(
        export_module,
        "CORPUS_OWNED_PATH_PREFIXES",
        tuple(
            prefix
            for prefix in CORPUS_OWNED_PATH_PREFIXES
            if prefix != "Agent/wiki/"
        ),
    )

    wiki = tmp_path / "Agent" / "wiki"
    wiki.mkdir(parents=True)
    for name in ("_index.md", "compiled-page.md"):
        (wiki / name).write_text("written by another librarian\n", encoding="utf-8")

    # A corpus of notes only -- nothing under Agent/wiki/.
    report = export_to(tmp_path, (make_document(),), apply=True, prune=True)

    assert report.prunable == []
    assert report.pruned == 0
    assert (wiki / "_index.md").exists()
    assert (wiki / "compiled-page.md").exists()


def test_the_wiki_prefix_is_owned_and_owned_implies_exported() -> None:
    """``Agent/wiki/`` is owned, and nothing is owned that is not also exported.

    The second assertion is the one with teeth. Prune walks the owned prefixes
    and keeps whatever the export accounted for, so a prefix that is owned but
    never written has an empty expected set -- and every file under it becomes
    an orphan on the next ``--apply --prune``.

    **The ordering that made this change safe is deliberately not asserted
    here, because no unit test can observe it.** ``Agent/wiki/`` was held out
    while the Stage-A librarian's fourteen pages existed only as files;
    ``scripts/import_vault_wiki.py --apply`` made them rows, and the prefix
    joined the owned set afterwards. That was a one-time deployment sequence
    against a live database. What this pins is the end state it produced, not
    the route taken to it.
    """

    assert "Agent/wiki/" in CORPUS_OWNED_PATH_PREFIXES
    assert set(CORPUS_OWNED_PATH_PREFIXES) <= set(EXPORTED_PATH_PREFIXES)


def test_prune_still_sweeps_a_prefix_the_corpus_does_populate(tmp_path: Path) -> None:
    """The guard narrows the sweep; it does not disable it."""

    retired = make_document(
        id="deaddeaddeaddeaddeaddeaddeaddead",
        vault_path="Agent/notes/deaddeaddeaddeaddeaddeaddeaddead.md",
    )
    export_to(tmp_path, (make_document(), retired), apply=True)

    report = export_to(tmp_path, (make_document(),), apply=True, prune=True)

    assert report.pruned == 1
    assert not (tmp_path / retired.vault_path).exists()


def test_a_candidate_is_projected_into_the_promotion_folder(
    tmp_path: Path,
) -> None:
    """Routing is ``vault_path``, and the exporter writes wherever it points.

    ADR 0010 requires the column to be byte-identical to the governance
    scanner's ``rel_path``, so the export cannot re-derive a directory from
    ``promotion_status`` without putting the row and the file under different
    ``folders.yml`` rules. ``VaultPromotionService`` moves the two together;
    this module just writes.
    """

    candidate = make_document(
        id="0bad0bad0bad0bad0bad0bad0bad0bad",
        vault_path="Agent/Promotion Candidates/worth-promoting.md",
        promotion_status=PromotionStatus.CANDIDATE,
    )

    report = export_to(tmp_path, (make_document(), candidate), apply=True)

    written = tmp_path / "Agent" / "Promotion Candidates" / "worth-promoting.md"
    assert report.written == 2
    assert written.exists()


def test_the_last_candidate_settling_empties_the_folder(tmp_path: Path) -> None:
    """The concrete bug ADR 0023 names, and the reason occupancy was wrong.

    Promote the only candidate and the folder has zero rows. An occupancy test
    reads that as "the corpus is not authoritative here" and skips the sweep,
    stranding a file that still advertises a candidacy which ended. Ownership
    does not move when the last row leaves.
    """

    candidate = make_document(
        id="0bad0bad0bad0bad0bad0bad0bad0bad",
        vault_path="Agent/Promotion Candidates/worth-promoting.md",
        promotion_status=PromotionStatus.CANDIDATE,
    )
    export_to(tmp_path, (make_document(), candidate), apply=True)
    stranded = tmp_path / "Agent" / "Promotion Candidates" / "worth-promoting.md"
    assert stranded.exists()

    # Promoted: the row is back under Agent/notes/, and nothing populates the
    # candidates prefix any more.
    promoted = make_document(
        id="0bad0bad0bad0bad0bad0bad0bad0bad",
        vault_path="Agent/notes/worth-promoting.md",
        promotion_status=PromotionStatus.PROMOTED,
    )
    report = export_to(
        tmp_path, (make_document(), promoted), apply=True, prune=True
    )

    assert report.prunable == [
        "Agent/Promotion Candidates/worth-promoting.md"
    ]
    assert report.pruned == 1
    assert not stranded.exists()


def test_every_owned_prefix_is_one_the_export_also_writes() -> None:
    """Sweeping a folder the export never writes would delete unconditionally.

    The owned set is a subset of the exported set by construction, and this is
    the assertion that keeps it one when either constant grows.
    """

    assert set(CORPUS_OWNED_PATH_PREFIXES) <= set(EXPORTED_PATH_PREFIXES)


# ------------------------------------------------------- the wiki index ----


def make_page(**overrides: object) -> VaultDocument:
    """A compiled wiki page, with the provenance its CHECK constraint wants."""

    page = VaultDocument(
        id="cafe0001",
        kind=DocumentKind.WIKI,
        status=DocumentStatus.ACTIVE,
        vault_path="Agent/wiki/idempotency-and-identity.md",
        title="Idempotency and Identity",
        body="Synthesis.",
        summary="What makes a request the same request.",
        contributed_by="agent:librarian",
        provenance={},
        schema_version=1,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
        doc_type="Wiki Page",
        doc_status="Current",
        compile_run_id=UUID("11111111-1111-1111-1111-111111111111"),
        compiled_by="agent:librarian",
        compiled_at=UPDATED_AT,
    )
    return replace(page, **overrides)  # type: ignore[arg-type]


def test_no_pages_means_no_index() -> None:
    """Writing "_0 notes._" would create a file to describe nothing.

    Absent also lets the prune guard remove a stale index once `Agent/wiki/`
    becomes a corpus-owned prefix.
    """

    assert render_wiki_index([make_document()]) is None


def test_the_index_lists_every_page_with_its_summary() -> None:
    index = render_wiki_index([make_document(), make_page()])

    assert index is not None
    assert index.vault_path == "Agent/wiki/_index.md"
    assert "_1 notes._" in index.content
    assert (
        "- [[idempotency-and-identity]] - What makes a request the same request."
        in index.content
    )
    # A note is not a page, whatever prefix it sits under.
    assert make_document().title not in index.content


def test_index_entries_sort_by_slug_case_insensitively() -> None:
    """What the Stage-A renderer does, and what keeps the file from churning
    when a title changes but its path does not."""

    pages = [
        make_page(id="a", vault_path="Agent/wiki/Zebra.md", title="Z"),
        make_page(id="b", vault_path="Agent/wiki/apple.md", title="A"),
    ]

    index = render_wiki_index(pages)

    assert index is not None
    assert index.content.index("[[apple]]") < index.content.index("[[Zebra]]")


def test_index_timestamps_come_from_the_pages_not_the_clock() -> None:
    """The module's whole contract is a zero-line diff on an unchanged corpus.

    Stage A stamped `LastUpdated` with `now()` and read the previous file to
    preserve `CreatedAt`. Neither is available here -- this module parses no
    markdown -- so both are derived: earliest page and latest page.
    """

    early = make_page(id="a", vault_path="Agent/wiki/a.md")
    late = make_page(
        id="b",
        vault_path="Agent/wiki/b.md",
        created_at=CREATED_AT + timedelta(days=2),
        updated_at=UPDATED_AT + timedelta(days=2),
    )

    index = render_wiki_index([early, late])

    assert index is not None
    assert f"CreatedAt: {utc_timestamp(CREATED_AT)}" in index.content
    assert f"LastUpdated: {utc_timestamp(UPDATED_AT + timedelta(days=2))}" in index.content


def test_index_timestamps_follow_origin_for_an_imported_page() -> None:
    """So the index agrees with the files it indexes.

    An imported page projects its upstream dates rather than the moment its row
    landed; an index reporting the import instead would disagree with every
    entry in it.
    """

    imported = make_page(
        origin={
            "created_at": "2026-08-13T18:56:41Z",
            "updated_at": "2026-08-13T18:56:41Z",
        }
    )

    index = render_wiki_index([imported])

    assert index is not None
    assert "CreatedAt: 2026-08-13T18:56:41Z" in index.content
    assert "LastUpdated: 2026-08-13T18:56:41Z" in index.content


def test_the_index_is_written_and_never_pruned(tmp_path: Path) -> None:
    """It is generated rather than stored, so no row accounts for it.

    That is exactly what the prune sweep looks for, which is why the export has
    to add it to the expected set rather than only writing it.
    """

    documents = (make_document(), make_page())

    first = export_to(tmp_path, documents, apply=True, prune=True)
    index = tmp_path / "Agent" / "wiki" / "_index.md"

    assert index.exists()
    assert first.prunable == []

    second = export_to(tmp_path, documents, apply=True, prune=True)

    assert second.written == 0
    assert second.unchanged == 3
    assert index.exists()


def test_the_walk_is_read_at_repeatable_read(tmp_path: Path) -> None:
    """Sharing a transaction is not what makes the paged walk consistent.

    Under the server default, READ COMMITTED, every statement takes a fresh
    snapshot -- so the loop saw the corpus move between pages while its
    docstring claimed it could not. The cursor makes that worse rather than
    better: `vault_path` is mutable, and promotion exists to move one, so a
    document can cross the cursor and be exported twice under two paths or not
    at all. Since the same walk feeds both the writes and the prune set, an
    `--apply --prune` run could then delete a document's old export without
    having written its new one.

    Asserted on what the exporter *asks the transaction for*, because that is
    the entire fix and it is invisible in the output otherwise -- a passing
    export proves nothing about isolation when nothing is writing concurrently.
    """

    transactions = _StubTransactions()
    service = VaultExportService(
        transactions,  # type: ignore[arg-type]
        _StubDocuments((make_document(),)),  # type: ignore[arg-type]
        page_size=2,
    )

    asyncio.run(service.export(tmp_path))

    assert transactions.isolation_levels == ["REPEATABLE READ"]
