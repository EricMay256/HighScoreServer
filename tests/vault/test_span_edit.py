"""Naming a span instead of authoring hunks.

Pure text handling, so none of this needs a database. The cases that matter
are the refusals: a span edit exists to remove the arithmetic a model gets
wrong, and it would be worthless if it guessed instead when the span is
unclear.
"""

import pytest

from app.vault.body_diff import (
    BodyDiffError,
    SpanEditError,
    apply_body_unified_diff,
    apply_span_edit,
    span_edit_to_unified_diff,
)


BODY = (
    "Alpha line one.\n"
    "Beta line two.\n"
    "Gamma line three.\n"
    "Delta line four.\n"
    "Epsilon line five.\n"
)


def test_a_unique_span_is_replaced() -> None:
    updated = apply_span_edit(
        BODY,
        expected_text="Beta line two.",
        replacement_text="Beta line two, amended.",
    )

    assert "Beta line two, amended." in updated
    assert "Beta line two.\n" not in updated
    # Nothing else moved.
    assert updated.count("\n") == BODY.count("\n")


def test_an_absent_span_is_refused_rather_than_fuzzily_matched() -> None:
    """No nearest-match, no whitespace tolerance. The remedy is to re-fetch."""

    with pytest.raises(SpanEditError, match="does not appear"):
        apply_span_edit(
            BODY,
            expected_text="Beta line  two.",  # two spaces
            replacement_text="anything",
        )


def test_an_ambiguous_span_is_refused_by_default() -> None:
    """The default must not silently mean "the first one".

    `occurrence=None` is the caller who has not considered duplicates, and the
    safe reading of that caller is that they believe the text is unique. If it
    is not, they need to know.
    """

    body = "repeat me\nsomething else\nrepeat me\n"

    with pytest.raises(SpanEditError, match="appears 2 times"):
        apply_span_edit(body, expected_text="repeat me", replacement_text="changed")


def test_occurrence_disambiguates_and_selects_the_named_one() -> None:
    body = "repeat me\nmiddle\nrepeat me\n"

    first = apply_span_edit(
        body, expected_text="repeat me", replacement_text="X", occurrence=1
    )
    second = apply_span_edit(
        body, expected_text="repeat me", replacement_text="X", occurrence=2
    )

    assert first == "X\nmiddle\nrepeat me\n"
    assert second == "repeat me\nmiddle\nX\n"


def test_an_out_of_range_occurrence_says_how_many_there_are() -> None:
    body = "repeat me\nrepeat me\n"

    with pytest.raises(SpanEditError, match="appears 2 times"):
        apply_span_edit(
            body, expected_text="repeat me", replacement_text="X", occurrence=3
        )


def test_an_edit_that_changes_nothing_is_refused() -> None:
    """An empty diff is not applicable, so it must fail here with a reason."""

    with pytest.raises(BodyDiffError, match="identical"):
        apply_span_edit(
            BODY,
            expected_text="Beta line two.",
            replacement_text="Beta line two.",
        )


def test_an_empty_expected_text_is_refused() -> None:
    with pytest.raises(SpanEditError, match="must not be empty"):
        apply_span_edit(BODY, expected_text="", replacement_text="X")


def test_a_span_may_cover_several_lines() -> None:
    updated = apply_span_edit(
        BODY,
        expected_text="Beta line two.\nGamma line three.\n",
        replacement_text="Beta and Gamma, merged.\n",
    )

    assert "Beta and Gamma, merged." in updated
    assert "Gamma line three." not in updated


def test_a_crlf_span_matches_an_lf_body() -> None:
    """The stored body decides the convention; the caller's span is normalized.

    A span copied out of a note through a Windows client arrives CRLF-mangled,
    which would otherwise fail to match a body it is character-for-character
    identical to.
    """

    updated = apply_span_edit(
        BODY,
        expected_text="Beta line two.\r\nGamma line three.\r\n",
        replacement_text="Merged.\r\n",
    )

    assert updated == BODY.replace("Beta line two.\nGamma line three.\n", "Merged.\n")


# ------------------------------------------------- the diff it produces ----


def test_the_rendered_diff_applies_back_through_the_strict_applier() -> None:
    """The property the whole design rests on.

    A span edit is stored as an ordinary body diff, so what this renders has
    to satisfy `apply_body_unified_diff` exactly -- same hunk grammar, same
    context matching, same policy. If that round trip broke, span edits would
    be a second, weaker patch format wearing the first one's clothes.
    """

    diff = span_edit_to_unified_diff(
        BODY,
        expected_text="Delta line four.",
        replacement_text="Delta line four, corrected.",
    )
    applied = apply_body_unified_diff(BODY, diff)

    assert applied.body == apply_span_edit(
        BODY,
        expected_text="Delta line four.",
        replacement_text="Delta line four, corrected.",
    )
    assert applied.hunk_count == 1


def test_the_rendered_diff_reports_the_removal_a_reviewer_must_acknowledge() -> None:
    """Replacing a line removes it, and the review path must see that.

    A span edit must not become a way to delete content without tripping the
    acknowledgement ADR 0028 requires.
    """

    diff = span_edit_to_unified_diff(
        BODY,
        expected_text="Gamma line three.\n",
        replacement_text="",
    )
    applied = apply_body_unified_diff(BODY, diff)

    assert [line.text for line in applied.removed_lines] == ["Gamma line three."]


def test_overlapping_matches_count_as_matches() -> None:
    """`str.count` counts non-overlapping runs; selection stepped by one.

    Two policies in one function meant "aa" in "aaa" reported a single match,
    passed the uniqueness check, and was edited at the first of two candidate
    offsets -- guessing, which ADR 0033 exists to prevent. Overlapping is the
    safer reading of "every place this span begins": it can only refuse more.
    """

    with pytest.raises(SpanEditError, match="appears 2 times"):
        apply_span_edit("aaa", expected_text="aa", replacement_text="X")


def test_overlapping_occurrences_are_selectable_and_bounded() -> None:
    """The same list that counts is the list that selects, so occurrence 2
    reaches the second overlapping span rather than being refused as absent."""

    assert (
        apply_span_edit("aaa", expected_text="aa", replacement_text="X", occurrence=1)
        == "Xa"
    )
    assert (
        apply_span_edit("aaa", expected_text="aa", replacement_text="X", occurrence=2)
        == "aX"
    )
    with pytest.raises(SpanEditError, match="out of range"):
        apply_span_edit("aaa", expected_text="aa", replacement_text="X", occurrence=3)


def test_a_self_overlapping_pattern_reports_every_starting_offset() -> None:
    """More than two, so the fix is not an off-by-one that happens to work."""

    # "aaaa" contains "aa" starting at 0, 1 and 2.
    with pytest.raises(SpanEditError, match="appears 3 times"):
        apply_span_edit("aaaa", expected_text="aa", replacement_text="X")

    assert (
        apply_span_edit("aaaa", expected_text="aa", replacement_text="X", occurrence=3)
        == "aaX"
    )


def test_a_lone_carriage_return_is_refused_rather_than_silently_rewritten() -> None:
    """A bare CR is a line boundary to `splitlines`.

    So the stored diff would apply "x\ny" for a requested "x\ry" -- a proposal
    describing text the caller never asked for. CRLF is still normalized
    against the stored body; this is what survives that normalization.
    """

    with pytest.raises(SpanEditError, match="carriage return"):
        apply_span_edit(BODY, expected_text="Beta line two.", replacement_text="x\ry")

    with pytest.raises(SpanEditError, match="carriage return"):
        apply_span_edit(BODY, expected_text="Beta\rline two.", replacement_text="x")


def test_changing_the_trailing_newline_state_is_refused() -> None:
    """The diff grammar has no no-newline marker and the applier keeps the
    original's trailing-newline state, so this cannot be carried by the stored
    artifact. Refused rather than accepted and dropped."""

    with pytest.raises(SpanEditError, match="ends with a newline"):
        span_edit_to_unified_diff("abc", expected_text="abc", replacement_text="xyz\n")

    with pytest.raises(SpanEditError, match="ends with a newline"):
        span_edit_to_unified_diff(
            "abc\n", expected_text="abc\n", replacement_text="xyz"
        )


@pytest.mark.parametrize(
    ("body", "expected_text", "replacement_text"),
    [
        (BODY, "Beta line two.", "Beta line two, revised."),
        (BODY, "Beta line two.\n", ""),
        (BODY, "Gamma line three.", "One.\nTwo.\nThree."),
        # A CRLF-stored body with the caller's span written either way.
        (BODY.replace("\n", "\r\n"), "Beta line two.", "Revised."),
        (BODY.replace("\n", "\r\n"), "Beta line two.\r\n", "Revised.\r\n"),
    ],
)
def test_the_stored_diff_reproduces_the_requested_text(
    body: str, expected_text: str, replacement_text: str
) -> None:
    """The invariant the feature rests on, asserted directly.

    A span edit is only "another authoring form for the same artifact" if the
    artifact applies to exactly what was asked for. `span_edit_to_unified_diff`
    now round-trips before storing; this pins that the check is real across the
    shapes that reach it.
    """

    intended = apply_span_edit(
        body, expected_text=expected_text, replacement_text=replacement_text
    )
    diff = span_edit_to_unified_diff(
        body, expected_text=expected_text, replacement_text=replacement_text
    )

    applied = apply_body_unified_diff(body.replace("\r\n", "\n"), diff)

    assert applied.body == intended
