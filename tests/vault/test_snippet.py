"""The lead extract: what it leads with, and what it refuses to lead with.

Pure text handling, so none of this needs a database. The cases that matter
are the ones where the first block of a document is not the document's claim
-- a heading, a code listing, a table -- because that is where a naive "first
240 characters" produces a preview that is worse than none.
"""

import pytest

from app.vault.snippet import (
    SNIPPET_MAX_CHARS,
    _strip_markdown_noise,
    lead_snippet,
)


def test_a_short_opening_paragraph_is_returned_whole() -> None:
    body = (
        "A rate-limit decorator on a FastAPI endpoint cannot protect work done\n"
        "in that endpoint's dependencies.\n\nThe failure is silent."
    )

    assert lead_snippet(body) == (
        "A rate-limit decorator on a FastAPI endpoint cannot protect work done "
        "in that endpoint's dependencies."
    )


def test_a_leading_heading_is_skipped_for_the_paragraph_under_it() -> None:
    """A title is a label. The claim is the sentence after it."""

    body = "# Guard ordering\n\nA decorator runs last, so it cannot guard.\n"

    assert lead_snippet(body) == "A decorator runs last, so it cannot guard."


def test_a_leading_code_block_is_skipped() -> None:
    """The corpus opens notes with error text; a preview of it says nothing.

    Both fence styles and the indented form, because all three appear in the
    corpus and only the fenced ones are obvious.
    """

    fenced = "```\nerror: patch failed: folders.yml:10\n```\n\nCRLF broke the hunks."
    tilde = "~~~\nerror: patch failed\n~~~\n\nCRLF broke the hunks."
    indented = "    error: patch failed: folders.yml:10\n\nCRLF broke the hunks."

    for body in (fenced, tilde, indented):
        assert lead_snippet(body) == "CRLF broke the hunks."


def test_a_table_is_not_a_preview() -> None:
    body = "| status | meaning |\n| --- | --- |\n\nFlagged is not a failure."

    assert lead_snippet(body) == "Flagged is not a failure."


def test_a_list_item_can_be_the_lead() -> None:
    """Excluded blocks are the misleading ones, not everything non-prose.

    A note whose first block is a bulleted claim still has a claim, and
    dropping the bullet marker is enough to show it.
    """

    body = "- Retrying a flagged contribution writes a second note.\n"

    assert lead_snippet(body) == (
        "Retrying a flagged contribution writes a second note."
    )


def test_a_block_quote_can_be_the_lead() -> None:
    body = "> Search before you contribute.\n"

    assert lead_snippet(body) == "Search before you contribute."


def test_markdown_emphasis_and_links_are_flattened() -> None:
    body = (
        "Dependencies resolve *before* the handler, so see "
        "[ADR 0021](docs/adr/0021.md) and [[Guard Ordering]] for `why`.\n"
    )

    assert lead_snippet(body) == (
        "Dependencies resolve before the handler, so see ADR 0021 and "
        "Guard Ordering for why."
    )


def test_a_long_paragraph_is_cut_at_a_word_boundary_and_marked() -> None:
    body = " ".join(["mechanism"] * 100)

    snippet = lead_snippet(body)

    assert snippet is not None
    assert len(snippet) <= SNIPPET_MAX_CHARS
    assert snippet.endswith("…")
    # Marked *and* whole-worded: the reader can tell it was clipped, and the
    # clip did not invent a word.
    assert not snippet.removesuffix("…").endswith("mechani")


def test_an_unbroken_run_longer_than_the_budget_is_still_cut() -> None:
    """No space inside the budget must not mean returning the whole thing."""

    body = "x" * 500

    snippet = lead_snippet(body)

    assert snippet is not None
    assert len(snippet) <= SNIPPET_MAX_CHARS


def test_a_body_with_no_prose_has_no_snippet() -> None:
    """None rather than "", so a caller can tell absent from empty."""

    assert lead_snippet("```\njust code\n```\n") is None
    assert lead_snippet("# Only a heading\n") is None
    assert lead_snippet("") is None


def test_internal_whitespace_is_collapsed() -> None:
    """A hard-wrapped paragraph is one sentence, not four lines."""

    body = "A claim\nwrapped   across\nseveral\nlines.\n"

    assert lead_snippet(body) == "A claim wrapped across several lines."


def test_a_hit_may_carry_neither_summary_nor_snippet() -> None:
    """The contract the schema used to overpromise.

    `lead_snippet` declines a body with no prose, and the write path accepts
    such a body, so a note that is entirely a table or a code listing and has
    no authored summary produces a hit with both fields null. The schema said
    "one of them will answer"; it now says the title may be all there is, and
    this pins the case that made the old wording false.
    """

    for body in (
        "```python\nx = 1\n```",
        "# Title\n\n## Section",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
        "    indented = 'code'",
    ):
        assert lead_snippet(body) is None, body


# --- Stage 1E: fenced regions and literal identifiers ---


def test_a_blank_line_inside_a_fence_does_not_end_the_code_block() -> None:
    """Splitting on blank lines first loses the fence state at the first blank
    line *inside* the code, and a blank line inside a Python or SQL listing is
    ordinary. The second half of the listing then read as fresh prose, so a
    note opening with a code block previewed as its own source.
    """

    body = (
        "```python\n"
        "setup()\n"
        "\n"
        "TEST_DATABASE_URL = value\n"
        "```\n"
        "\n"
        "Actual claim."
    )

    assert lead_snippet(body) == "Actual claim."


def test_tilde_fences_are_tracked_like_backtick_fences() -> None:
    body = (
        "~~~sql\n"
        "SELECT 1;\n"
        "\n"
        "SELECT 2;\n"
        "~~~\n"
        "\n"
        "The claim."
    )

    assert lead_snippet(body) == "The claim."


def test_a_longer_fence_is_closed_only_by_one_at_least_as_long() -> None:
    """Which is what lets a four-backtick block contain a three-backtick one.
    Treating any fence as a closer would end the outer block early and expose
    its contents."""

    body = (
        "````\n"
        "```\n"
        "nested code\n"
        "```\n"
        "````\n"
        "\n"
        "The claim."
    )

    assert lead_snippet(body) == "The claim."


def test_an_unclosed_fence_swallows_the_rest_rather_than_leaking_code() -> None:
    """A body that opens a fence and never closes it is malformed. Preferring
    no preview to a preview of source is the same call `lead_snippet` already
    makes for a body that is entirely code."""

    assert lead_snippet("```\ncode\n\nmore code") is None


def test_prose_before_a_fence_is_still_preferred() -> None:
    assert lead_snippet("The claim.\n\n```\ncode\n```") == "The claim."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The failure this fixes: a blanket delete of `*_~` mangled every
        # snake_case identifier, and an identifier is one of the strongest
        # selection signals a preview of an engineering note carries.
        ("TEST_DATABASE_URL is required", "TEST_DATABASE_URL is required"),
        ("Use jsonb_path_ops for containment", "Use jsonb_path_ops for containment"),
        # Inline code keeps its contents exactly, delimiters removed.
        ("Set `max_length` on the field", "Set max_length on the field"),
        ("`TEST_DATABASE_URL`", "TEST_DATABASE_URL"),
        # Emphasis still flattens, which is why the rule exists at all.
        ("*emphasis* and **bold**", "emphasis and bold"),
        ("_leading emphasis_ here", "leading emphasis here"),
        ("~~struck~~ text", "struck text"),
        # A lone delimiter with no partner is literal text, not markup.
        ("2 * 3 = 6", "2 * 3 = 6"),
        ("a_b_c_d", "a_b_c_d"),
    ],
)
def test_markdown_cleanup_keeps_literal_identifiers(raw: str, expected: str) -> None:
    assert _strip_markdown_noise(raw) == expected


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_a_content_line_starting_with_a_fence_does_not_close_the_block(
    fence: str,
) -> None:
    """A closing fence carries nothing but its markers.

    An opening fence may carry an info string, so matching a prefix for both
    meant a line of *content* beginning with backticks ended the block, and the
    code under it became the preview -- the same leak the fence tracking was
    added to stop, through the other side.
    """

    body = (
        f"{fence}python\n"
        f"{fence}not-a-close\n"
        "SECRET_CODE\n"
        f"{fence}\n"
        "\n"
        "Actual claim."
    )

    assert lead_snippet(body) == "Actual claim."


def test_a_closing_fence_may_be_indented_or_trail_whitespace() -> None:
    """The allowances that were already there, kept while tightening the rest."""

    assert lead_snippet("```\ncode\n   ```\n\nActual claim.") == "Actual claim."
    assert lead_snippet("```\ncode\n```   \n\nActual claim.") == "Actual claim."


@pytest.mark.parametrize(
    "identifier", ["__init__", "__aexit__", "__enter__", "__all__"]
)
def test_dunder_identifiers_survive_the_emphasis_rules(identifier: str) -> None:
    """Structurally these are emphasis, and CommonMark renders `__init__` as
    bold `init`. This corpus is about Python and Postgres, where they are
    names, and a preview naming `init` describes a symbol that does not exist.
    """

    assert _strip_markdown_noise(f"{identifier} is the hook") == (
        f"{identifier} is the hook"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A delimiter run has to be whole on both sides. Consuming one marker
        # from each pair produced text nobody wrote.
        ("x ~~ y ~~ z", "x ~~ y ~~ z"),
        ("x ** y ** z", "x ** y ** z"),
        # An identifier-shaped token keeps its underscores.
        ("_private_thing_", "_private_thing_"),
        # Ordinary emphasis still flattens, which is the point of the rule.
        ("_emphasis_ matters", "emphasis matters"),
        ("_a whole phrase_ here", "a whole phrase here"),
        ("**bold**", "bold"),
        ("***triple***", "triple"),
        ("~~struck~~", "struck"),
        # Inline code wins first, so a backticked dunder comes out exact.
        ("`__init__`", "__init__"),
    ],
)
def test_emphasis_is_removed_only_when_it_is_unmistakably_emphasis(
    raw: str, expected: str
) -> None:
    assert _strip_markdown_noise(raw) == expected
