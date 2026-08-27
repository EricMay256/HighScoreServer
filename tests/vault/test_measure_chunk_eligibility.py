"""The chunk-eligibility measurement's structure detection.

Only the pure half is tested here -- the database half is one `select` with the
same predicates search uses, and testing it would be testing SQLAlchemy.

What is worth testing is the part that can be quietly wrong. This script feeds
a decision about whether to build chunking (vault ADR 0034), and it argues that
decision from heading counts and section sizes. A `#` inside a fenced shell
block counted as a heading would make undivided notes look sectioned, and
"eligible" is defined as long *and* multi-sectioned -- so a false heading is a
false argument for chunking, in the direction the script is meant to guard
against.
"""

from scripts.measure_chunk_eligibility import shape_of, strip_fenced_blocks


def test_a_hash_inside_a_fence_is_not_a_heading() -> None:
    """A shell comment is code, not structure.

    The realistic false positive: the corpus is full of notes whose bodies
    carry shell transcripts, and `# do the thing` is how they are annotated.
    """

    body = (
        "The mechanism, stated once.\n\n"
        "```bash\n"
        "# install it\n"
        "## still a comment\n"
        "pip install thing\n"
        "```\n\n"
        "## A real heading\n\n"
        "More prose here.\n"
    )

    shape = shape_of("d1", "note", "T", body)

    assert shape.headings == 1


def test_a_tilde_fence_masks_as_well_as_a_backtick_fence() -> None:
    body = "Lead.\n\n~~~\n# not a heading\n~~~\n\n# a heading\n\nBody.\n"

    assert shape_of("d1", "note", "T", body).headings == 1


def test_an_unclosed_fence_masks_to_the_end_of_the_document() -> None:
    """What a markdown renderer does with one, so what this does too."""

    body = "Lead.\n\n```\n# not a heading\n\n# also not a heading\n"

    assert shape_of("d1", "note", "T", body).headings == 0


def test_masking_preserves_offsets() -> None:
    """Section sizes are measured against the masked text by offset.

    Deleting fenced regions rather than blanking them would shift every later
    index, so a document's sections would be measured against a document that
    does not exist.
    """

    body = "abc\n\n```\nxyz\n```\n\ndef\n"

    masked = strip_fenced_blocks(body)

    assert len(masked) == len(body)
    assert masked.count("\n") == body.count("\n")
    assert "xyz" not in masked


def test_prose_before_the_first_heading_counts_as_a_section() -> None:
    """In this corpus that opening paragraph is the thesis.

    Dropping it would undercount exactly the most retrievable part of a
    document.
    """

    body = "The claim, at length.\n\n# One\n\nBody one.\n\n# Two\n\nBody two.\n"

    shape = shape_of("d1", "wiki", "T", body)

    assert shape.headings == 2
    assert len(shape.section_characters) == 3


def test_an_unsectioned_document_is_one_section() -> None:
    body = "A single undivided argument.\n"

    shape = shape_of("d1", "note", "T", body)

    assert shape.headings == 0
    assert shape.section_characters == (len(body),)
    assert shape.substantial_sections == 0


def test_only_substantial_sections_count_toward_eligibility() -> None:
    """Stub headings are structure without anything to retrieve.

    A document carved into one-line sections has headings and nothing worth
    addressing separately; counting those would make it look chunkable.
    """

    stubs = "# a\n\nx\n\n# b\n\ny\n\n# c\n\nz\n"
    real = "# a\n\n" + ("word " * 120) + "\n\n# b\n\n" + ("word " * 120) + "\n"

    assert shape_of("d1", "wiki", "T", stubs).substantial_sections == 0
    assert shape_of("d2", "wiki", "T", real).substantial_sections == 2


def test_token_estimate_tracks_characters() -> None:
    """Estimated, and the docstring says so; this pins the ratio it claims."""

    shape = shape_of("d1", "note", "T", "x" * 400)

    assert shape.tokens == 100


def test_an_empty_body_has_no_sections() -> None:
    shape = shape_of("d1", "note", "T", "")

    assert shape.characters == 0
    assert shape.section_characters == ()
    assert shape.substantial_sections == 0
