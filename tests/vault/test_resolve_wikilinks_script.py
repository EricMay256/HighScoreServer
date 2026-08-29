"""Repairing ``related_ids`` rows that hold wikilinks instead of ids.

The planning is pure and pinned directly. The final test drives the dry run and
apply paths against the configured test database, because what this has to get
right is a rewrite of a live column: resolving to the wrong document reads as a
working citation, and dropping a link without preserving it first destroys the
only record that the page ever cited anything.

The production case is a wiki page whose ``Related`` names a *sibling page by
title* while the file it lives in is named for the slug -- so the link resolves
by title and by nothing else.
"""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest

import scripts.resolve_vault_wikilinks as resolve_script
from app.vault.wikilinks import LinkIndex, LinkTarget
from scripts.resolve_vault_wikilinks import _report, plan, run


class _Row:
    """A ``vault_documents`` row as ``plan`` reads it."""

    def __init__(
        self,
        id: str,
        kind: str,
        vault_path: str,
        related_ids: list[str],
        frontmatter: dict | None = None,
    ) -> None:
        self.id = id
        self.kind = kind
        self.vault_path = vault_path
        self.related_ids = related_ids
        self.frontmatter = frontmatter or {}


def _index() -> LinkIndex:
    return LinkIndex(
        [LinkTarget("target-id", "Operating the Agent Knowledge Vault", "operating")]
    )


def test_a_wikilink_becomes_an_id_and_the_original_is_preserved() -> None:
    row = _Row("page", "wiki", "Agent/wiki/page.md", ["[[Operating the Agent Knowledge Vault]]"])

    [change] = plan([row], _index())

    assert change.resolution.values == ("target-id",)
    # ADR 0025 drops an unresolved name on the grounds that it survives in
    # `frontmatter`. These rows' frontmatter was empty, so the premise is made
    # true rather than assumed.
    assert change.preserve_key == "Related"
    assert change.frontmatter == {
        "Related": ["[[Operating the Agent Knowledge Vault]]"]
    }


def test_a_note_preserves_under_its_own_governance_key() -> None:
    row = _Row("note", "note", "Agent/notes/note.md", ["[[operating]]"])

    [change] = plan([row], _index())

    assert change.preserve_key == "RelatedIDs"


def test_a_row_that_already_holds_ids_produces_no_change() -> None:
    """What makes a second run write nothing."""

    row = _Row("page", "wiki", "Agent/wiki/page.md", ["target-id"])

    assert plan([row], _index()) == []


def test_an_existing_frontmatter_copy_is_not_overwritten() -> None:
    """The older evidence wins. A repair must not clobber what an import kept."""

    row = _Row(
        "page",
        "wiki",
        "Agent/wiki/page.md",
        ["[[operating]]"],
        frontmatter={"Related": ["[[Something Older]]"]},
    )

    [change] = plan([row], _index())

    assert change.resolution.values == ("target-id",)
    assert change.preserve_key is None
    assert change.frontmatter is None


def test_a_bare_title_is_repaired_the_same_as_a_wikilink() -> None:
    """The values `export._warnings` names, which this could not previously fix.

    A plain title, a stray single bracket and a padded name are all the same
    corruption as a stored `[[Title]]`. The exporter warns about each and names
    this script as the repair, so each has to actually be repairable here.
    """

    for stored in (
        "Operating the Agent Knowledge Vault",
        "[Operating the Agent Knowledge Vault]",
        "  Operating the Agent Knowledge Vault  ",
    ):
        row = _Row("page", "wiki", "Agent/wiki/page.md", [stored])

        [change] = plan([row], _index())

        assert change.resolution.values == ("target-id",), stored
        assert change.resolution.malformed == (stored,), stored
        # Still preserved before rewriting, exactly as a wikilink would be.
        assert change.frontmatter == {"Related": [stored]}, stored


def test_a_bare_title_naming_nothing_is_still_dropped_not_kept_as_an_id() -> None:
    row = _Row("page", "wiki", "Agent/wiki/page.md", ["Nobody Wrote This"])

    [change] = plan([row], _index())

    assert change.resolution.values == ()
    assert change.resolution.dropped == ("Nobody Wrote This",)


def test_a_differing_frontmatter_copy_refuses_the_row_when_a_link_would_drop() -> None:
    """The gap in "older evidence wins".

    The existing copy is evidence of a *different* list, so it does not stand in
    for the one about to lose a link. Preserving it and dropping anyway would
    leave the dropped name nowhere, which is the one thing the preserve step
    exists to prevent.
    """

    row = _Row(
        "page",
        "wiki",
        "Agent/wiki/page.md",
        ["[[Nobody Wrote This]]"],
        frontmatter={"Related": ["[[Something Older]]"]},
    )

    [change] = plan([row], _index())

    assert change.conflict is not None
    assert "Related" in change.conflict
    # Nothing is written to a refused row, so the preserve step stays off too.
    assert change.preserve_key is None
    assert change.frontmatter is None


def test_a_differing_frontmatter_copy_is_fine_when_nothing_drops() -> None:
    """The boundary: a rewrite that only resolves loses nothing, so the older
    evidence can stay untouched and the row is still repaired."""

    row = _Row(
        "page",
        "wiki",
        "Agent/wiki/page.md",
        ["[[operating]]"],
        frontmatter={"Related": ["[[Something Older]]"]},
    )

    [change] = plan([row], _index())

    assert change.conflict is None
    assert change.resolution.values == ("target-id",)


def test_an_ambiguous_row_is_reported_even_though_nothing_is_rewritten() -> None:
    """A link naming two documents is the one outcome that needs a human, so the
    row has to survive into the report rather than being filtered out as a
    no-change row."""

    index = LinkIndex(
        [LinkTarget("a", "Shared", "shared-a"), LinkTarget("b", "Shared", "shared-b")]
    )
    row = _Row("page", "wiki", "Agent/wiki/page.md", ["[[Shared]]"])

    [change] = plan([row], index)

    assert not change.resolution.changed
    assert change.resolution.ambiguous == (("[[Shared]]", ("a", "b")),)
    assert change.preserve_key is None


def test_dry_run_then_apply_resolves_the_exact_database_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry run writes nothing; apply turns the link into the sibling's id."""

    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    citing = "test-wikilink-citing"
    target = "test-wikilink-target"
    compile_run = uuid4()

    connection = psycopg.connect(test_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault.vault_compile_runs
                    (id, compiler_principal_id, state, completed_at)
                VALUES (%s, 'agent:test', 'succeeded', now())
                """,
                (compile_run,),
            )
            cursor.execute(
                """
                INSERT INTO vault.vault_documents
                    (id, kind, doc_type, vault_path, status, doc_status,
                     title, body, source_ids, related_ids, contributed_by,
                     schema_version, compile_run_id, compiled_by, compiled_at)
                VALUES
                    (%s, 'wiki', 'Wiki Page', %s, 'active', 'Current',
                     %s, 'Body', '{}', '{}', 'agent:test', 1,
                     %s, 'agent:test', now()),
                    (%s, 'wiki', 'Wiki Page', %s, 'active', 'Current',
                     'Citing page', 'Body', '{}', %s, 'agent:test', 1,
                     %s, 'agent:test', now())
                """,
                (
                    target,
                    # The title and the file name deliberately disagree, which
                    # is the corpus: the link resolves by title alone.
                    "Agent/wiki/test-wikilink-slug-differs.md",
                    "Operating the Test Vault",
                    compile_run,
                    citing,
                    "Agent/wiki/test-wikilink-citing.md",
                    ["[[Operating the Test Vault]]", "[[Nobody Wrote This]]"],
                    compile_run,
                ),
            )
        connection.commit()

        assert asyncio.run(run(apply=False)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT related_ids FROM vault.vault_documents WHERE id = %s",
                (citing,),
            )
            assert cursor.fetchone() == (
                ["[[Operating the Test Vault]]", "[[Nobody Wrote This]]"],
            )

        assert asyncio.run(run(apply=True)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT related_ids, frontmatter FROM vault.vault_documents WHERE id = %s",
                (citing,),
            )
            related_ids, frontmatter = cursor.fetchone()
        assert related_ids == [target]
        assert frontmatter == {
            "Related": ["[[Operating the Test Vault]]", "[[Nobody Wrote This]]"]
        }

        # The second apply is the idempotency assertion at the database
        # boundary: it finds nothing to resolve and leaves the row untouched.
        assert asyncio.run(run(apply=True)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT related_ids FROM vault.vault_documents WHERE id = %s",
                (citing,),
            )
            assert cursor.fetchone() == ([target],)
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id IN (%s, %s)",
                (citing, target),
            )
            cursor.execute(
                "DELETE FROM vault.vault_compile_runs WHERE id = %s",
                (compile_run,),
            )
        connection.commit()
        connection.close()


def test_apply_refuses_a_row_a_governed_update_changed_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost update this script used to perform.

    It reads at READ COMMITTED and holds no corpus lock, so a governed update
    can commit between the plan and the write. The write names the values it
    planned from, so the row is skipped rather than reverted -- and because the
    repair is idempotent, skipping costs a rerun and reverting would cost the
    other writer's edit.
    """

    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    citing = "test-race-citing"
    target = "test-race-target"
    compile_run = uuid4()
    committed_by_someone_else = ["deliberately-not-the-planned-value"]

    connection = psycopg.connect(test_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault.vault_compile_runs
                    (id, compiler_principal_id, state, completed_at)
                VALUES (%s, 'agent:test', 'succeeded', now())
                """,
                (compile_run,),
            )
            cursor.execute(
                """
                INSERT INTO vault.vault_documents
                    (id, kind, doc_type, vault_path, status, doc_status,
                     title, body, source_ids, related_ids, contributed_by,
                     schema_version, compile_run_id, compiled_by, compiled_at)
                VALUES
                    (%s, 'wiki', 'Wiki Page', %s, 'active', 'Current',
                     %s, 'Body', '{}', '{}', 'agent:test', 1,
                     %s, 'agent:test', now()),
                    (%s, 'wiki', 'Wiki Page', %s, 'active', 'Current',
                     'Citing page', 'Body', '{}', %s, 'agent:test', 1,
                     %s, 'agent:test', now())
                """,
                (
                    target,
                    "Agent/wiki/test-race-target.md",
                    "Operating the Raced Vault",
                    compile_run,
                    citing,
                    "Agent/wiki/test-race-citing.md",
                    ["[[Operating the Raced Vault]]"],
                    compile_run,
                ),
            )
        connection.commit()

        # The race, made deterministic: commit a competing update in the window
        # between the plan and the writes it drives.
        real_plan = resolve_script.plan

        def plan_then_let_someone_else_win(rows, index):
            changes = real_plan(rows, index)
            racer = psycopg.connect(test_url)
            try:
                with racer.cursor() as cursor:
                    cursor.execute(
                        "UPDATE vault.vault_documents SET related_ids = %s "
                        "WHERE id = %s",
                        (committed_by_someone_else, citing),
                    )
                racer.commit()
            finally:
                racer.close()
            return changes

        monkeypatch.setattr(
            resolve_script, "plan", plan_then_let_someone_else_win
        )

        assert asyncio.run(run(apply=True)) == 1

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT related_ids, frontmatter FROM vault.vault_documents "
                "WHERE id = %s",
                (citing,),
            )
            related_ids, frontmatter = cursor.fetchone()
        # The other writer's edit stands, and nothing was preserved over it.
        assert related_ids == committed_by_someone_else
        assert frontmatter == {}
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id IN (%s, %s)",
                (citing, target),
            )
            cursor.execute(
                "DELETE FROM vault.vault_compile_runs WHERE id = %s",
                (compile_run,),
            )
        connection.commit()
        connection.close()


def test_a_remaining_ambiguity_exits_nonzero(capsys) -> None:
    """An ambiguous value is a *name* in a column whose contract is ids.

    The exporter omits names, so a repair that reports success while leaving
    one behind invites the next export to drop the very relationship the
    repair was run to preserve. Ambiguity needs a human, and the exit code is
    how a caller learns there is one waiting.
    """

    index = LinkIndex(
        [
            LinkTarget("id-a", "Shared Title", "one"),
            LinkTarget("id-b", "Shared Title", "two"),
        ]
    )
    row = _Row("page", "wiki", "Agent/wiki/page.md", ["[[Shared Title]]"])

    changes = plan([row], index)

    assert _report(changes, [], applied=False) == 1
    assert "name more than one document" in capsys.readouterr().out


def test_a_dry_run_says_apply_re_evaluates_the_corpus(capsys) -> None:
    """The plan is a preview, not a promise: apply takes the corpus lock and
    resolves against whatever the corpus is then."""

    _report([], [], applied=False)

    output = capsys.readouterr().out
    assert "corpus lock" in output
    assert "preview, not a promise" in output


def test_a_clean_run_still_exits_zero(capsys) -> None:
    """The boundary: nothing ambiguous, refused or stale is success."""

    assert _report([], [], applied=True) == 0
