"""Fence masking in the chunk-eligibility measurement.

The counts this feeds decide whether ADR 0034's chunking deferral still holds,
so a misparsed fence is not a cosmetic bug -- it moves the number the decision
is made on. The scanner is shared with `snippet` for exactly that reason: two
copies had two different wrong answers.
"""

import pytest

from scripts.measure_chunk_eligibility import shape_of, strip_fenced_blocks


def _headings(body: str) -> int:
    return shape_of("id", "note", "T", body).headings


def test_offsets_are_preserved_so_section_sizes_stay_true() -> None:
    """Blanking rather than deleting is what keeps later indices meaningful."""

    body = "# One\n\n```\ncode\n```\n\n# Two\n"

    assert len(strip_fenced_blocks(body)) == len(body)


def test_a_longer_outer_fence_contains_a_shorter_run() -> None:
    """Pairing fence lines by position read the inner ``` as the close, made
    the real ```` a fresh unclosed opener, and from there counted headings
    inside code while masking genuine ones after the block."""

    body = "````\n```\n# Not a heading\n```\n````\n\n# A real heading\n"

    assert _headings(body) == 1


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fence_prefixed_content_does_not_close_the_block(fence: str) -> None:
    body = (
        f"{fence}python\n"
        f"{fence}not-a-close\n"
        "# Not a heading\n"
        f"{fence}\n"
        "\n"
        "# A real heading\n"
    )

    assert _headings(body) == 1


def test_a_close_must_use_the_opening_character() -> None:
    """A tilde line inside a backtick block is content."""

    body = "```\n~~~\n# Not a heading\n~~~\n```\n\n# A real heading\n"

    assert _headings(body) == 1


def test_trailing_whitespace_after_a_close_is_allowed() -> None:
    body = "```\n# Not a heading\n```   \n\n# A real heading\n"

    assert _headings(body) == 1


def test_an_unclosed_fence_masks_to_the_end_of_the_document() -> None:
    """Which is what a renderer does with one, so the measurement should agree."""

    body = "# A real heading\n\n```\n# Not a heading\n"

    assert _headings(body) == 1


def test_a_heading_immediately_after_a_close_is_counted() -> None:
    """No blank line between them, which is where an off-by-one would show."""

    body = "```\ncode\n```\n# A real heading\n"

    assert _headings(body) == 1
