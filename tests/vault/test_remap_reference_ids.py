"""Resolving a reference id across import generations.

The small tests pin the resolution itself, because every way it can be wrong is
silent. The final test drives the dry run and apply paths against the configured
test database: mapping to the wrong note reads as a working citation, and
dropping an unresolvable one destroys the only evidence the note was ever cited.

The production case that motivated this is the two-hop one: a note contributed
against the *pre-wipe* service ids cites ids that appear in no current map, and
resolve only by composing the old map's pairs with the new map's.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from scripts.remap_vault_reference_ids import (
    IdentityClasses,
    load_maps,
    remap,
    resolve,
    run,
)


STAGE_A = "221cd423c28d45ce9d6613075c6d4296"
PRE_WIPE = "660c590c277142b9baab03528cc1420d"
LIVE = "9711ac5985974fcdbfe0c33aa071d390"


def _classes(*pairs: tuple[str, str]) -> IdentityClasses:
    classes = IdentityClasses()
    for left, right in pairs:
        classes.union(left, right)
    return classes


def test_one_hop_resolves_a_stage_a_id_to_the_live_note() -> None:
    resolution = resolve(_classes((STAGE_A, LIVE)), {LIVE})

    assert resolution.canonical[STAGE_A] == LIVE
    assert remap([STAGE_A], resolution.canonical) == [LIVE]


@pytest.mark.parametrize("reverse", [False, True], ids=["new-first", "old-first"])
def test_two_hops_compose_whichever_order_the_maps_arrive_in(reverse: bool) -> None:
    # The pre-wipe id shares a class with the live id only through the Stage-A
    # id they both map from. Neither map alone can resolve it.
    pairs = [(STAGE_A, PRE_WIPE), (STAGE_A, LIVE)]
    if reverse:
        pairs.reverse()

    resolution = resolve(_classes(*pairs), {LIVE})

    assert resolution.canonical[PRE_WIPE] == LIVE
    assert resolution.canonical[STAGE_A] == LIVE
    assert not resolution.orphaned


def test_remapping_is_idempotent_so_a_second_run_writes_nothing() -> None:
    resolution = resolve(_classes((STAGE_A, PRE_WIPE), (STAGE_A, LIVE)), {LIVE})

    once = remap([PRE_WIPE, STAGE_A, LIVE], resolution.canonical)
    assert once == [LIVE, LIVE, LIVE]
    assert remap(once, resolution.canonical) == once


def test_a_class_with_no_live_id_is_reported_not_dropped() -> None:
    resolution = resolve(_classes((STAGE_A, PRE_WIPE)), {"some-other-live-note"})

    assert resolution.canonical == {}
    assert list(resolution.orphaned.values()) == [{STAGE_A, PRE_WIPE}]
    # The reference survives the remap unchanged rather than being deleted.
    assert remap([PRE_WIPE], resolution.canonical) == [PRE_WIPE]


def test_two_live_ids_in_one_class_is_refused_rather_than_guessed() -> None:
    second = "ffffffffffffffffffffffffffffffff"
    resolution = resolve(_classes((STAGE_A, LIVE), (STAGE_A, second)), {LIVE, second})

    assert resolution.canonical == {}
    assert list(resolution.ambiguous.values()) == [{LIVE, second}]


def test_order_and_duplicates_are_preserved() -> None:
    other_stage_a, other_live = "aaaa", "bbbb"
    resolution = resolve(
        _classes((STAGE_A, LIVE), (other_stage_a, other_live)), {LIVE, other_live}
    )

    assert remap([other_stage_a, STAGE_A, other_stage_a], resolution.canonical) == [
        other_live,
        LIVE,
        other_live,
    ]


def test_load_maps_composes_generations_from_disk(tmp_path: Path) -> None:
    new = tmp_path / "import-map.json"
    old = tmp_path / "old-map.json"
    new.write_text(json.dumps({STAGE_A: {"note_id": LIVE, "slug": "x.md"}}), "utf-8")
    old.write_text(
        json.dumps({STAGE_A: {"note_id": PRE_WIPE, "slug": "x.md"}}), "utf-8"
    )

    resolution = resolve(load_maps([new, old]), {LIVE})

    assert resolution.canonical[PRE_WIPE] == LIVE


def test_dry_run_then_apply_repoints_the_exact_database_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry run writes nothing; apply fixes wiki sources and note relations."""

    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    stage_a = "test-remap-stage-a"
    pre_wipe = "test-remap-pre-wipe"
    live = "test-remap-live"
    wiki = "test-remap-wiki"
    citing_note = "test-remap-citing-note"
    compile_run = uuid4()
    paths = [
        "Agent/notes/test-remap-live.md",
        "Agent/wiki/test-remap-page.md",
        "Agent/notes/test-remap-citing-note.md",
    ]

    current_map = tmp_path / "current-map.json"
    old_map = tmp_path / "old-map.json"
    current_map.write_text(json.dumps({stage_a: {"note_id": live}}), "utf-8")
    old_map.write_text(json.dumps({stage_a: {"note_id": pre_wipe}}), "utf-8")

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
                    (%s, 'note', 'Agent Note', %s, 'active', 'Active',
                     'Live note', 'Body', '{}', '{}', 'agent:test', 1,
                     NULL, NULL, NULL),
                    (%s, 'wiki', 'Wiki Page', %s, 'active', 'Current',
                     'Wiki page', 'Body', %s, %s, 'agent:test', 1,
                     %s, 'agent:test', now()),
                    (%s, 'note', 'Agent Note', %s, 'active', 'Active',
                     'Citing note', 'Body', '{}', %s, 'agent:test', 1,
                     NULL, NULL, NULL)
                """,
                (
                    live,
                    paths[0],
                    wiki,
                    paths[1],
                    [stage_a],
                    ["[[A Related Wiki Page]]"],
                    compile_run,
                    citing_note,
                    paths[2],
                    [pre_wipe],
                ),
            )
        connection.commit()

        assert asyncio.run(run([current_map, old_map], apply=False)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT source_ids FROM vault.vault_documents WHERE id = %s",
                (wiki,),
            )
            assert cursor.fetchone() == ([stage_a],)

        assert asyncio.run(run([current_map, old_map], apply=True)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, source_ids, related_ids
                FROM vault.vault_documents
                WHERE id IN (%s, %s)
                ORDER BY id
                """,
                (citing_note, wiki),
            )
            rows = cursor.fetchall()
        assert rows == [
            (citing_note, [], [live]),
            (wiki, [live], ["[[A Related Wiki Page]]"]),
        ]

        # The second apply is the actual idempotency assertion at the database
        # boundary: it finds no changes and leaves the rows untouched.
        assert asyncio.run(run([current_map, old_map], apply=True)) == 0
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id IN (%s, %s, %s)",
                (wiki, citing_note, live),
            )
            cursor.execute(
                "DELETE FROM vault.vault_compile_runs WHERE id = %s",
                (compile_run,),
            )
        connection.commit()
        connection.close()
