"""Backfilling authored summaries onto notes that lack one.

The pairing logic is pure and pinned directly. The last test drives the dry-run
and apply paths against the configured test database, because the thing this
has to get right is a re-embed: writing the summary without moving the vector
and its hash together would leave a row whose `embedded_text_sha256` agrees
with text nobody embedded, and nothing in the codebase repairs that afterwards.
"""

import asyncio
import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app.vault.constants import EMBEDDING_DIMENSIONS
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.embeddings import EmbeddingInputKind, EmbeddingVector
from scripts.backfill_vault_summaries import (
    MAX_SUMMARY_CHARS,
    WORK_FILE_SCHEMA_VERSION,
    Authored,
    Undescribed,
    embeddable,
    emit,
    governance_problems,
    read_work_file,
    run,
)


PROFILE_ID = "test/backfill-model:1536"


class StubProvider:
    """Deterministic embeddings, so the digest assertion is not incidental."""

    profile_id = PROFILE_ID
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(
        self, texts, kind: EmbeddingInputKind
    ) -> tuple[EmbeddingVector, ...]:
        del kind
        self.texts.extend(texts)
        return tuple(self._vector(text) for text in texts)

    @staticmethod
    def _vector(text: str) -> EmbeddingVector:
        axis = int.from_bytes(sha256(text.encode("utf-8")).digest()[:4], "big")
        axis %= EMBEDDING_DIMENSIONS
        return tuple(
            1.0 if index == axis else 0.0 for index in range(EMBEDDING_DIMENSIONS)
        )

    async def aclose(self) -> None:
        return None


def _note(document_id: str = "note-1", **overrides) -> Undescribed:
    base = {
        "document_id": document_id,
        "vault_path": f"Agent/notes/{document_id}.md",
        "title": "A note that nobody described",
        "body": "A body long enough to satisfy the governance minimum.",
        "tags": ("testing",),
        "aliases": (),
        "contributed_by": "agent:test",
        "content_revision": 1,
    }
    return Undescribed(**{**base, **overrides})


def _work_file(tmp_path: Path, entries: list[dict]) -> Path:
    """A current-schema work file. Entries default to the revision `_note`
    reports, so a test that cares about staleness has to say so."""

    path = tmp_path / "work.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": WORK_FILE_SCHEMA_VERSION,
                "notes": [{"content_revision": 1, **entry} for entry in entries],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_an_authored_summary_is_paired_with_its_row(tmp_path: Path) -> None:
    path = _work_file(tmp_path, [{"id": "note-1", "summary": "What it establishes."}])

    work, skipped, problems = read_work_file(path, [_note()])

    assert (skipped, problems) == ([], [])
    assert [(item.note.document_id, item.summary) for item in work] == [
        ("note-1", "What it establishes.")
    ]


def test_a_blank_summary_is_skipped_without_complaint(tmp_path: Path) -> None:
    """Leaving an entry empty is how an author declines a note, not an error."""

    path = _work_file(
        tmp_path,
        [{"id": "note-1", "summary": "   "}, {"id": "note-2", "summary": ""}],
    )

    work, skipped, problems = read_work_file(path, [_note(), _note("note-2")])

    assert work == []
    assert (skipped, problems) == ([], [])


def test_a_note_summarized_since_the_emit_is_reported_and_skipped(
    tmp_path: Path,
) -> None:
    """The emit-then-author gap is open for as long as authoring takes.

    A note that gained a summary in between is no longer in the plan, so its
    id no longer resolves -- and the authored text must not overwrite whatever
    landed there.
    """

    path = _work_file(tmp_path, [{"id": "note-gone", "summary": "Written anyway."}])

    work, skipped, problems = read_work_file(path, [_note()])

    assert work == []
    assert problems == []
    assert skipped == [
        "note-gone: already summarized, or no longer an active note"
    ]


def test_a_summary_authored_against_an_older_revision_is_refused(
    tmp_path: Path,
) -> None:
    """Re-reading the corpus fixes the vector and not the sentence.

    The author wrote its precis against the body the emit exported. If the note
    was rewritten since, pairing that precis with the current text gives a row
    whose vector and digest agree perfectly and whose summary is about a
    document that no longer exists. Nothing downstream can catch that, because
    nothing downstream knows what the summary was written about.

    A problem rather than a skip: it needs re-authoring, not a rerun.
    """

    path = _work_file(
        tmp_path,
        [{"id": "note-1", "summary": "Describes the note as it used to read."}],
    )

    work, skipped, problems = read_work_file(path, [_note(content_revision=4)])

    assert work == []
    assert skipped == []
    assert len(problems) == 1
    assert "authored against revision 1" in problems[0]
    assert "now at 4" in problems[0]
    assert "re-author" in problems[0].lower()


def test_a_work_file_without_revisions_is_refused_rather_than_assumed_current(
    tmp_path: Path,
) -> None:
    """The absence of the field is indistinguishable from "it has not moved",
    and guessing that wrongly is the whole failure this prevents."""

    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"notes": [{"id": "note-1", "summary": "Written earlier."}]}),
        encoding="utf-8",
    )

    work, skipped, problems = read_work_file(path, [_note()])

    assert (work, skipped) == ([], [])
    assert len(problems) == 1
    assert "schema_version" in problems[0]


def test_an_emitted_work_file_records_the_revision_it_was_read_at(
    tmp_path: Path,
) -> None:
    """The emit half of the same contract: without this the comparison above
    has nothing to compare against."""

    path = tmp_path / "work.json"
    emit([_note(content_revision=7)], path)

    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema_version"] == WORK_FILE_SCHEMA_VERSION
    assert document["notes"][0]["content_revision"] == 7
    # And the round trip is accepted, so emit and read cannot drift apart.
    work, _skipped, problems = read_work_file(
        _work_file(tmp_path, [{"id": "note-1", "summary": "Fine.", "content_revision": 7}]),
        [_note(content_revision=7)],
    )
    assert problems == []
    assert len(work) == 1


def test_an_over_long_summary_fails_in_the_dry_run(tmp_path: Path) -> None:
    """The bound is the write boundary's, checked early rather than at the last
    statement of an apply that has already spent its embedding calls."""

    path = _work_file(
        tmp_path, [{"id": "note-1", "summary": "x" * (MAX_SUMMARY_CHARS + 1)}]
    )

    work, _skipped, problems = read_work_file(path, [_note()])

    assert work == []
    assert len(problems) == 1
    assert "over the 2000" in problems[0]


def test_governance_validation_runs_over_the_resulting_note(tmp_path: Path) -> None:
    """The candidate is validated as a whole document, not as a loose string."""

    path = _work_file(tmp_path, [{"id": "note-1", "summary": "Fine."}])
    work, _skipped, _problems = read_work_file(path, [_note(tags=("ok", "ok"))])

    assert governance_problems(work) == ["note-1: duplicate tags are not allowed"]


def test_the_candidate_carries_the_summary_into_the_embedding_text() -> None:
    """The summary has to reach `assemble_embedding_text`, or the backfill would
    re-embed the note unchanged and buy nothing."""

    candidate = embeddable(Authored(note=_note(), summary="The precis."))

    assert "The precis." in assemble_embedding_text(candidate)


def test_dry_run_then_apply_writes_the_summary_and_its_vector_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    monkeypatch.setenv("VAULT_EMBEDDING_API_KEY", "test-key")

    stub = StubProvider()
    monkeypatch.setattr(
        "scripts.backfill_vault_summaries.create_embedding_provider",
        lambda settings: stub,
    )

    note_id = f"test-backfill-{uuid4().hex[:8]}"
    summary = "Establishes that the backfill moves the vector with the summary."

    connection = psycopg.connect(test_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault.vault_documents
                    (id, kind, doc_type, vault_path, status, doc_status,
                     title, body, tags, aliases, source_ids, related_ids,
                     contributed_by, schema_version)
                VALUES
                    (%s, 'note', 'Agent Note', %s, 'active', 'Active',
                     %s, %s, %s, '{}', '{}', '{}', 'agent:test', 2)
                """,
                (
                    note_id,
                    f"Agent/notes/{note_id}.md",
                    "A note contributed without a summary",
                    "A body long enough to satisfy the governance minimum.",
                    ["testing"],
                ),
            )
        connection.commit()

        work = tmp_path / "work.json"
        work.write_text(
            json.dumps(
                {
                    "schema_version": WORK_FILE_SCHEMA_VERSION,
                    "notes": [
                        {"id": note_id, "summary": summary, "content_revision": 1}
                    ],
                }
            ),
            encoding="utf-8",
        )

        assert asyncio.run(run(None, work, apply=False)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT summary FROM vault.vault_documents WHERE id = %s", (note_id,)
            )
            assert cursor.fetchone() == (None,)
        assert stub.texts == [], "a dry run must not spend an embedding call"

        assert asyncio.run(run(None, work, apply=True)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.summary, d.content_revision, e.embedded_text_sha256
                FROM vault.vault_documents d
                LEFT JOIN vault.vault_document_embeddings e
                    ON e.document_id = d.id AND e.profile_id = %s
                WHERE d.id = %s
                """,
                (PROFILE_ID, note_id),
            )
            stored_summary, revision, digest = cursor.fetchone()

        assert stored_summary == summary
        # Caller-supplied content moved, so an amendment composed against the
        # old revision must go stale rather than apply over this (ADR 0028).
        assert revision == 2
        # The hash describes the text that was actually embedded. This is the
        # assertion the whole script exists to keep true.
        assert bytes(digest) == embedding_text_digest(stub.texts[0])
        assert digest is not None

        # A second apply finds the summary present and leaves it alone, which
        # is what makes an interrupted run safe to re-run.
        before = stub.texts.copy()
        assert asyncio.run(run(None, work, apply=True)) == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT summary, content_revision FROM vault.vault_documents "
                "WHERE id = %s",
                (note_id,),
            )
            assert cursor.fetchone() == (summary, 2)
        assert stub.texts == before, "a re-run must not re-embed a settled note"
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_document_embeddings WHERE document_id = %s",
                (note_id,),
            )
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id = %s", (note_id,)
            )
        connection.commit()
        connection.close()


def test_a_note_edited_after_embedding_is_skipped_not_indexed_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backfill's half of the same race as the carveout's.

    The apply reloads a snapshot, embeds it outside any transaction, then
    writes. A content update committing in that window used to be overwritten
    at the summary column while the vector still described the older title and
    body -- and `embedded_text_sha256` would agree with the vector, so nothing
    afterwards could tell.

    The provider is the hook, because embedding *is* the window.
    """

    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    monkeypatch.setenv("VAULT_EMBEDDING_API_KEY", "test-key")

    note_id = f"test-backfill-race-{uuid4().hex[:8]}"
    summary = "Establishes that a stale snapshot is refused rather than written."

    connection = psycopg.connect(test_url)

    class RacingProvider(StubProvider):
        async def embed(self, texts, kind):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault.vault_documents
                    SET body = %s, content_revision = content_revision + 1
                    WHERE id = %s
                    """,
                    ("Someone else rewrote this body entirely.", note_id),
                )
            connection.commit()
            return await super().embed(texts, kind)

    stub = RacingProvider()
    monkeypatch.setattr(
        "scripts.backfill_vault_summaries.create_embedding_provider",
        lambda settings: stub,
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault.vault_documents
                    (id, kind, doc_type, vault_path, status, doc_status,
                     title, body, tags, aliases, source_ids, related_ids,
                     contributed_by, schema_version)
                VALUES
                    (%s, 'note', 'Agent Note', %s, 'active', 'Active',
                     %s, %s, %s, '{}', '{}', '{}', 'agent:test', 2)
                """,
                (
                    note_id,
                    f"Agent/notes/{note_id}.md",
                    "A note that changes under the backfill",
                    "A body long enough to satisfy the governance minimum.",
                    ["testing"],
                ),
            )
        connection.commit()

        work = tmp_path / "work.json"
        work.write_text(
            json.dumps(
                {
                    "schema_version": WORK_FILE_SCHEMA_VERSION,
                    "notes": [
                        {"id": note_id, "summary": summary, "content_revision": 1}
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Nonzero: work was planned and deliberately not done, which an
        # operator has to be able to notice without reading the output.
        assert asyncio.run(run(None, work, apply=True)) == 1

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.summary, e.embedded_text_sha256
                FROM vault.vault_documents d
                LEFT JOIN vault.vault_document_embeddings e
                    ON e.document_id = d.id AND e.profile_id = %s
                WHERE d.id = %s
                """,
                (PROFILE_ID, note_id),
            )
            stored_summary, digest = cursor.fetchone()

        # Nothing was written, so the other writer's body stands alone and no
        # vector claims to describe it.
        assert stored_summary is None
        assert digest is None
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_document_embeddings WHERE document_id = %s",
                (note_id,),
            )
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id = %s", (note_id,)
            )
        connection.commit()
        connection.close()


def test_a_note_rewritten_between_emit_and_apply_is_not_summarized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end, against the database: emit at N, rewrite to N+1, apply.

    The gap this covers is the long one -- authoring, not embedding. Nothing is
    written and no embedding call is spent, because the summary in the file
    describes a note that no longer reads that way.
    """

    test_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("VAULT_DATABASE_URL", test_url)
    monkeypatch.setenv("VAULT_EMBEDDING_API_KEY", "test-key")

    stub = StubProvider()
    monkeypatch.setattr(
        "scripts.backfill_vault_summaries.create_embedding_provider",
        lambda settings: stub,
    )

    note_id = f"test-backfill-emitgap-{uuid4().hex[:8]}"
    work = tmp_path / "work.json"

    connection = psycopg.connect(test_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault.vault_documents
                    (id, kind, doc_type, vault_path, status, doc_status,
                     title, body, tags, aliases, source_ids, related_ids,
                     contributed_by, schema_version)
                VALUES
                    (%s, 'note', 'Agent Note', %s, 'active', 'Active',
                     %s, %s, %s, '{}', '{}', '{}', 'agent:test', 2)
                """,
                (
                    note_id,
                    f"Agent/notes/{note_id}.md",
                    "A note about the original subject",
                    "The original body, which the summary will describe.",
                    ["testing"],
                ),
            )
        connection.commit()

        # 1. Emit, and author against what it exported.
        assert asyncio.run(run(work, None, apply=False)) == 0
        document = json.loads(work.read_text(encoding="utf-8"))
        entry = next(e for e in document["notes"] if e["id"] == note_id)
        assert entry["content_revision"] == 1
        entry["summary"] = "Establishes something about the original subject."
        work.write_text(json.dumps(document), encoding="utf-8")

        # 2. The note is rewritten while the summary is being authored.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE vault.vault_documents
                SET body = %s, title = %s,
                    content_revision = content_revision + 1
                WHERE id = %s
                """,
                (
                    "An entirely different body about an unrelated subject.",
                    "A note about something else",
                    note_id,
                ),
            )
        connection.commit()

        # 3. Apply refuses it.
        assert asyncio.run(run(None, work, apply=True)) == 1

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.summary, e.embedded_text_sha256
                FROM vault.vault_documents d
                LEFT JOIN vault.vault_document_embeddings e
                    ON e.document_id = d.id AND e.profile_id = %s
                WHERE d.id = %s
                """,
                (PROFILE_ID, note_id),
            )
            stored_summary, digest = cursor.fetchone()

        assert stored_summary is None
        assert digest is None
        assert stub.texts == [], "a refused entry must not spend an embedding call"
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vault.vault_document_embeddings WHERE document_id = %s",
                (note_id,),
            )
            cursor.execute(
                "DELETE FROM vault.vault_documents WHERE id = %s", (note_id,)
            )
        connection.commit()
        connection.close()
