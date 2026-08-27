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
    Authored,
    Undescribed,
    embeddable,
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
    }
    return Undescribed(**{**base, **overrides})


def _work_file(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "work.json"
    path.write_text(json.dumps({"notes": entries}), encoding="utf-8")
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
            json.dumps({"notes": [{"id": note_id, "summary": summary}]}),
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
