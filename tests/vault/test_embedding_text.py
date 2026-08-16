"""What gets embedded, and when the hash is allowed to change.

The hash exists to keep re-import and re-embed separate (ADR 0013), so the
tests that matter are the ones asserting which edits move it and which do not.
"""

from hashlib import sha256

from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.embedding_text import (
    assemble_embedding_text,
    digest_for,
    embedding_text_digest,
)


def document(**overrides) -> NewVaultDocument:
    base = {
        "id": "fixture",
        "kind": DocumentKind.NOTE,
        "vault_path": "Agent/notes/fixture.md",
        "status": DocumentStatus.ACTIVE,
        "title": "Reciprocal rank fusion",
        "body": "Combines two ranked lists without calibrating their scores.",
        "contributed_by": "test:embedding-text",
        "provenance": {},
    }
    return NewVaultDocument(**{**base, **overrides})


def test_assembled_text_carries_exactly_the_declared_fields() -> None:
    text = assemble_embedding_text(
        document(
            title="Reciprocal rank fusion",
            aliases=("RRF",),
            tags=("retrieval", "ranking"),
            summary="Fuses ranked lists by position.",
            body="Combines two ranked lists without calibrating their scores.",
        )
    )

    # Terms are sorted rather than left in frontmatter order — see the
    # reordering test below for why that is deliberate.
    assert text == (
        "Reciprocal rank fusion\n"
        "RRF\n"
        "ranking retrieval\n"
        "Fuses ranked lists by position.\n"
        "\n"
        "Combines two ranked lists without calibrating their scores."
    )


def test_absent_parts_are_omitted_rather_than_left_blank() -> None:
    """A document that later gains a summary must not merely shift whitespace.

    Emitting empty lines for missing parts would make the hash change when
    nothing about the meaning did.
    """

    text = assemble_embedding_text(document(title="Bare", body="Only a body."))

    assert text == "Bare\n\nOnly a body."
    assert "\n\n\n" not in text


def test_reordering_tags_or_aliases_does_not_change_the_hash() -> None:
    """Order in frontmatter is not meaning, so it must not cost an API call."""

    first = digest_for(
        document(tags=("ranking", "retrieval"), aliases=("RRF", "Fusion"))
    )
    second = digest_for(
        document(tags=("retrieval", "ranking"), aliases=("Fusion", "RRF"))
    )

    assert first == second


def test_duplicate_and_blank_terms_are_dropped() -> None:
    canonical = digest_for(document(tags=("retrieval",)))
    noisy = digest_for(document(tags=("retrieval", " retrieval ", "", "   ")))

    assert noisy == canonical


def test_changing_an_alias_changes_the_hash() -> None:
    """The case that justifies embedding aliases at all.

    An alias is how a note is found under another name, so editing one is a
    genuine change to what should be embedded.
    """

    before = digest_for(document(aliases=("RRF",)))
    after = digest_for(document(aliases=("RRF", "Rank fusion")))

    assert before != after


def test_fields_outside_the_template_do_not_change_the_hash() -> None:
    """The whole point of a separate hash from ``source_sha256``.

    A frontmatter or bookkeeping edit changes the file, so re-import is right —
    but it must not buy an embedding call. doc_type and doc_status in
    particular are excluded because they are filterable columns.
    """

    baseline = digest_for(document())

    for overrides in (
        {"doc_type": "Agent Note"},
        {"doc_status": "Stub"},
        {"frontmatter": {"Category": "Reference", "LastUpdated": "2026-07-29"}},
        {"related_ids": ("other-note",)},
        {"source_ids": ("upstream",)},
        {"source_url": "https://example.test/x"},
        {"contributed_by": "someone-else"},
        {"vault_path": "Human/17 Concepts/moved.md"},
        {"source_sha256": sha256(b"different file bytes").digest()},
    ):
        assert digest_for(document(**overrides)) == baseline, overrides


def test_the_digest_is_the_hash_of_the_assembled_text() -> None:
    """Assembly and hashing cannot drift apart.

    ``embedded_text_sha256`` is only meaningful if it hashes the same string
    that was sent to the provider.
    """

    doc = document(aliases=("RRF",), tags=("retrieval",), summary="A summary.")
    text = assemble_embedding_text(doc)

    assert digest_for(doc) == sha256(text.encode("utf-8")).digest()
    assert digest_for(doc) == embedding_text_digest(text)
    assert len(digest_for(doc)) == 32
