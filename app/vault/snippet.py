"""A bounded preview of a document, for choosing between search results.

Search returns candidates; something has to say what each candidate claims.
`summary` is the authored field for that and is the right one when it exists —
but it is authored, and across the corpus this serves it mostly does not
exist. Measured 2026-08-26: **3 of 70 notes carry a summary, against 14 of 15
wiki pages.** So for the note corpus the snippet is not a supplement to
`summary`, it is the only thing distinguishing one hit from another beyond the
title, and it has to be good enough to choose on.

**This is a lead extract, not a match highlight, and the distinction is the
design.** Postgres can highlight a lexical match with `ts_headline`, and it was
measured rather than dismissed. Two findings sent the design here.

A hit found only by the vector arm shares no vocabulary with the query by
construction, so there is nothing to highlight and `ts_headline` returns the
document's opening words regardless. Nothing can explain a semantic match.
Promising "why did this match?" in the schema would be a promise the vector arm
cannot keep, and a field meaning one thing for lexical hits and another for
semantic ones is worse than one meaning the same for both.

The second finding is the one that decided it, because it applies to hits the
lexical arm *did* find. ADR 0007 rewrites the tsquery's conjunctions to
disjunctions — right for ranking, since `ts_rank_cd` and RRF then weigh how
much each shared word is worth — but `ts_headline` has no ranking to defer to
and treats every lexeme alike. So a note sharing one incidental word with a
long question gets a fragment anchored on that word. Asking "how do I stop a
retry from creating a duplicate note" of a note about idempotency returns

    ...the server cannot tell the [retry] from a fresh submission.

where this function returns "An idempotency digest must depend on the request
alone." The lead extract simply wins, and it wins on the case a highlight was
supposed to be good at.

What a lead extract *can* promise is the note's own claim, which is the better
selection signal whichever arm found it. These notes are written thesis-first —
the title is a declarative sentence and the opening paragraph states the
mechanism — so the first paragraph is close to an authored summary that nobody
had to author.

A `ts_headline` arm remains possible and is not merely "call `ts_headline`":
it needs a document-frequency cut so weak terms cannot anchor a fragment, and
routing by retrieval arm does not substitute for that cut — the example above
matched lexically. ADR 0031 records the measurements and the gate for
revisiting.

Kept as its own module rather than folded into the response projection because
it is pure text handling with awkward edges (fences, headings, block quotes)
that deserve direct tests, and because such an arm would replace this one
function rather than a slice of `api_models`.
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
    # Inline code, keeping its contents exactly. First, so a backticked
    # identifier is settled before the emphasis rules can look inside it.
    text = re.sub(r"`+([^`]+)`+", r"\1", text)
    # Emphasis and strikethrough, as *pairs* rather than as characters.
    #
    # A blanket `[`*_~]` deletion turned `TEST_DATABASE_URL` into
    # `TESTDATABASEURL` and `jsonb_path_ops` into `jsonbpathops`. In a corpus
    # of engineering notes an identifier is one of the strongest selection
    # signals a preview carries, and a mangled one is worse than none: it
    # names a symbol that does not exist. Requiring a closing delimiter, and
    # requiring `_` emphasis to sit at a word boundary, leaves snake_case
    # alone -- an underscore inside a word is not emphasis in any Markdown
    # dialect, which is why CommonMark has the same rule.
    text = re.sub(r"(\*{1,3})(\S(?:.*?\S)?)\1", r"\2", text)
    text = re.sub(r"(?<![A-Za-z0-9_])(_{1,3})(\S(?:.*?\S)?)\1(?![A-Za-z0-9_])", r"\2", text)
    text = re.sub(r"(~{1,2})(\S(?:.*?\S)?)\1", r"\2", text)
    # Leading list bullets and quote markers on the first line only.
    return re.sub(r"^\s{0,3}([-+*]|\d+\.|>)\s+", "", text)


def _blocks_outside_fences(body: str) -> list[str]:
    """Blank-line-delimited blocks, with fenced regions removed entirely.

    Splitting on blank lines first and then testing each block's first line for
    a fence loses the fence state at the first blank line *inside* the code --
    and a blank line inside a fenced block is ordinary, especially in the
    Python and SQL listings this corpus is full of. The second half of such a
    block then looked like a fresh block of prose, so a listing's middle became
    the preview: a body opening with a fenced snippet containing one blank line
    previewed as its own source code rather than as the claim underneath it.

    Fence state is therefore tracked line by line, and a closing fence must be
    at least as long as the one that opened it and use the same character --
    which is what lets a ```` ```` ```` block contain a ``` ``` `` line.
    """

    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None

    for line in body.split("\n"):
        opening = _FENCE.match(line)
        if fence is None and opening is not None:
            # Everything gathered so far ends here; the fence starts a region
            # that contributes nothing.
            if current:
                blocks.append("\n".join(current))
                current = []
            fence = opening.group(1)
            continue
        if fence is not None:
            closing = _FENCE.match(line)
            if closing is not None:
                marker = closing.group(1)
                if marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
            continue
        if line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []

    if current:
        blocks.append("\n".join(current))
    return blocks


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

    for block in _blocks_outside_fences(body):
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
