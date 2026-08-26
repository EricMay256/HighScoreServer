"""A bounded preview of a document, for choosing between search results.

Search returns candidates; something has to say what each candidate claims.
`summary` is the authored field for that and is the right one when it exists —
but it is authored, and across the corpus this serves it mostly does not
exist. Measured 2026-08-26: **3 of 70 notes carry a summary, against 14 of 15
wiki pages.** So for the note corpus the snippet is not a supplement to
`summary`, it is the only thing distinguishing one hit from another beyond the
title, and it has to be good enough to choose on.

**This is a lead extract, not a match highlight, and the distinction is the
design.** Postgres can highlight a lexical match with `ts_headline`, and that
was considered. It cannot help the case that matters most here: a hit found
only by the vector arm shares no vocabulary with the query by construction —
"how do I stop a retry creating a duplicate" reaching a note titled "An
idempotency digest must depend on the request alone" — so there is nothing to
highlight and `ts_headline` falls back to the document's opening words
anyway. Neither approach can explain a semantic match. Promising "why did this
match?" in the schema would therefore be a promise the vector arm cannot keep,
and a snippet that means one thing for lexical hits and another for vector
hits is worse than one that means the same thing for both.

What a lead extract *can* promise is the note's own claim, which is the better
selection signal regardless of which arm found it. These notes are written
thesis-first — the title is a declarative sentence and the opening paragraph
states the mechanism — so the first paragraph is close to an authored summary
that nobody had to author.

Kept as its own module rather than folded into the response projection because
it is pure text handling with awkward edges (fences, headings, block quotes)
that deserve direct tests, and because a future `ts_headline` arm for lexical
hits would replace this one function rather than a slice of `api_models`.
"""

import re


# Chosen against the corpus rather than guessed, because it is multiplied by
# the page size on every search and so is a budget decision wearing a
# formatting decision's clothes.
#
# Measured over 85 documents on 2026-08-26, the *full* opening paragraph runs
# to a median of 313 characters, p75 450, p90 769. The share arriving
# uncut at each candidate ceiling:
#
#     240 -> 34%    320 -> 53%    400 -> 68%    480 -> 78%
#
# 320 is the widest setting that leaves the whole search response inside the
# 8 KiB structured ceiling once the ~300 bytes per hit of identifiers, title
# and ranking are counted: ten snippets cost at most 3.1 KiB, and about
# 2.7 KiB in practice. Going to 400 buys 15 more complete paragraphs and
# spends most of the remaining headroom to do it.
#
# A clipped snippet still identifies the note -- what it loses is the payoff
# clause, not the subject -- so completeness is worth having and not worth
# overspending on.
SNIPPET_MAX_CHARS = 320

# A fenced block opens and closes with at least three backticks or tildes.
# Matched on the fence itself rather than by parsing the document, because the
# only question here is which paragraphs to skip.
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# Setext underlines ("Title" over "=====") would otherwise read as a paragraph
# of punctuation.
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")


def _is_prose(block: str) -> bool:
    """Whether a block is worth showing as the lead.

    Excludes what would be actively misleading in a one-line preview rather
    than everything that is not a plain sentence. An indented code block, a
    table row, or a bare heading tells a reader nothing about the note's
    claim; a list item or a block quote often carries it, so both are kept.
    """

    # The first line with content, indentation intact. Stripping the block
    # first would defeat the indented-code test below -- four leading spaces
    # are the only thing marking that form, and `.strip()` removes exactly
    # them. That bug shipped in the first draft of this function and was
    # caught by the indented case in `test_a_leading_code_block_is_skipped`.
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return False
    first = lines[0]

    if _FENCE.match(first) or _SETEXT.match(first):
        return False
    # An indented code block. The corpus uses these for terminal output and
    # error text, which is exactly the content a preview should not lead with.
    if first.startswith("    ") or first.startswith("\t"):
        return False
    marker = first.lstrip()
    # A heading on its own is a label, not a claim.
    if marker.startswith("#"):
        return False
    # A table, a horizontal rule, or frontmatter that escaped its parser.
    if marker.startswith(("|", "---", "***", "___")):
        return False
    return True


def _strip_markdown_noise(text: str) -> str:
    """Flatten the markup a one-line preview cannot render.

    Deliberately shallow: emphasis and inline code markers are removed because
    they read as typos in a plain-text preview, while link *text* is kept and
    its target dropped. Nothing here tries to be a markdown parser — a preview
    that is 95% right is worth far more than the dependency a correct one
    would cost.
    """

    # [label](target) -> label, before any other rule can eat the brackets.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # [[Wikilink]] -> Wikilink.
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    # Inline code, bold, italic, strikethrough markers.
    text = re.sub(r"[`*_~]", "", text)
    # Leading list bullets and quote markers on the first line only.
    return re.sub(r"^\s{0,3}([-+*]|\d+\.|>)\s+", "", text)


def lead_snippet(body: str, *, limit: int = SNIPPET_MAX_CHARS) -> str | None:
    """The document's opening claim, collapsed to one bounded line.

    Returns ``None`` when the body has no prose to lead with — a page that is
    entirely a table or a code listing has no honest preview, and an empty
    string in the response would read as one that was computed and came back
    blank.

    Truncation cuts at a word boundary and marks itself with an ellipsis, so a
    reader can tell a complete opening sentence from a clipped one. That
    distinction matters when the snippet is the only thing being chosen on.
    """

    if not body:
        return None

    for block in re.split(r"\n\s*\n", body):
        if not _is_prose(block):
            continue
        text = " ".join(_strip_markdown_noise(block).split())
        if not text:
            continue
        if len(text) <= limit:
            return text
        # Cut inside the budget, then back off to the last space so the
        # snippet does not end mid-word. A block with no space inside the
        # budget (a long URL, a hash) is cut where it falls rather than
        # returned whole.
        clipped = text[: limit - 1]
        spaced = clipped.rsplit(" ", 1)[0]
        return (spaced if spaced else clipped) + "…"

    return None
