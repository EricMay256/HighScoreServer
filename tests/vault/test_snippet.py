"""The lead extract: what it leads with, and what it refuses to lead with.

Pure text handling, so none of this needs a database. The cases that matter
are the ones where the first block of a document is not the document's claim
-- a heading, a code listing, a table -- because that is where a naive "first
240 characters" produces a preview that is worse than none.
"""

from app.vault.snippet import SNIPPET_MAX_CHARS, lead_snippet


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
