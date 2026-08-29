"""Translating between ids and wikilinks at the corpus boundaries.

ADR 0025's rule is that inside the database every edge is an id. Every way this
can be wrong is silent: a name resolved to the wrong document reads exactly like
a working citation, a dropped one destroys the evidence that the note was ever
cited, and an id rendered as a wikilink the run cannot resolve produces a link
Obsidian shows as broken.

The production case that motivated this is the round trip: fourteen wiki pages
whose ``Related`` frontmatter reached ``related_ids`` verbatim, were re-exported
verbatim, and so looked correct from either end.
"""

import pytest

from app.vault.wikilinks import (
    LinkIndex,
    LinkTarget,
    looks_like_a_name,
    parse_wikilink,
    render_edges,
    resolve_edges,
    slug_of,
)


PAGE = "a1b2c3d4e5f6470880b1a2c3d4e5f601"
NOTE = "a1b2c3d4e5f6470880b1a2c3d4e5f602"
OTHER = "a1b2c3d4e5f6470880b1a2c3d4e5f603"


def _index() -> LinkIndex:
    return LinkIndex(
        [
            # Title and slug disagree, which is the real corpus: the page
            # titled "Calibrating a Semantic Dedup Threshold" lives at
            # `semantic-dedup-threshold-calibration.md`.
            LinkTarget(PAGE, "Calibrating a Semantic Dedup Threshold", "semantic-dedup"),
            LinkTarget(NOTE, "Two loaders, two answers", "two-loaders", ("The .env one",)),
        ]
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("[[Some Page]]", "Some Page"),
        ("  [[Some Page]]  ", "Some Page"),
        # Obsidian's display and heading suffixes name the same target.
        ("[[Some Page|display text]]", "Some Page"),
        ("[[Some Page#A Heading]]", "Some Page"),
        # An ordinary id is not a link, which is what makes this safe to run
        # over a whole column.
        (PAGE, None),
        ("", None),
        ("[[]]", None),
        # A mention inside prose is not a stored edge. Anchoring keeps a body
        # sentence that leaked into the column from becoming one.
        ("see [[Some Page]] for more", None),
    ],
)
def test_a_value_is_a_wikilink_or_it_is_not(value: str, expected: str | None) -> None:
    assert parse_wikilink(value) == expected


def test_the_slug_comes_from_the_path_not_the_title() -> None:
    """Obsidian resolves ``[[x]]`` against a file name, and the file is a slug.

    This is why the corpus's exported ``Related`` links were unfollowable even
    before anyone looked at the column: the file for "Operating the Agent
    Knowledge Vault" is ``operating-the-agent-knowledge-vault.md``, so a
    ``[[Title]]`` link into it resolves to nothing.
    """

    assert slug_of("Agent/wiki/operating-the-agent-knowledge-vault.md") == (
        "operating-the-agent-knowledge-vault"
    )


def test_a_title_resolves_even_when_the_slug_is_different() -> None:
    resolution = resolve_edges(["[[Calibrating a Semantic Dedup Threshold]]"], _index())

    assert resolution.values == (PAGE,)
    assert resolution.resolved == (
        ("[[Calibrating a Semantic Dedup Threshold]]", PAGE),
    )


@pytest.mark.parametrize(
    "link",
    [
        "[[semantic-dedup]]",
        "[[calibrating a semantic dedup threshold]]",
        # The loose tier: punctuation and spacing collapse through `slugify`,
        # so a link written with a colon still reaches the page.
        "[[Calibrating a Semantic Dedup: Threshold]]",
    ],
)
def test_a_slug_alias_or_punctuated_name_reaches_the_same_page(link: str) -> None:
    assert resolve_edges([link], _index()).values == (PAGE,)


def test_an_alias_resolves_because_it_is_another_name_for_the_node() -> None:
    assert resolve_edges(["[[The .env one]]"], _index()).values == (NOTE,)


def test_an_id_already_in_the_column_passes_through_untouched() -> None:
    """What makes a second repair run write nothing."""

    resolution = resolve_edges([PAGE, OTHER], _index())

    assert resolution.values == (PAGE, OTHER)
    assert not resolution.changed


def test_an_unresolvable_link_is_dropped_not_stored() -> None:
    """ADR 0025: an unresolved name is not an id, and ``related_ids`` holds ids."""

    resolution = resolve_edges([PAGE, "[[A Page Nobody Wrote]]"], _index())

    assert resolution.values == (PAGE,)
    assert resolution.dropped == ("[[A Page Nobody Wrote]]",)


def test_an_ambiguous_name_is_reported_and_left_exactly_as_it_was() -> None:
    """Two notes may legitimately share a title, so this is not a coin toss.

    Pointing the edge at whichever row sorted first would read as a working
    citation while naming the wrong note -- the failure this whole module is
    trying to make impossible.
    """

    index = LinkIndex(
        [LinkTarget(PAGE, "Shared Title", "one"), LinkTarget(NOTE, "Shared Title", "two")]
    )

    resolution = resolve_edges(["[[Shared Title]]"], index)

    assert resolution.values == ("[[Shared Title]]",)
    assert resolution.ambiguous == (("[[Shared Title]]", (PAGE, NOTE)),)
    assert not resolution.changed


def test_an_exact_title_beats_a_loose_punctuation_match() -> None:
    """The tiers are ordered, and the order is the point.

    The loose tier erases punctuation, so running it first would let a
    punctuation collision outrank a title somebody actually wrote.
    """

    index = LinkIndex(
        [
            LinkTarget(PAGE, "Windows: Byte-Exact Artifacts", "windows-a"),
            LinkTarget(NOTE, "Windows Byte Exact Artifacts", "windows-b"),
        ]
    )

    assert resolve_edges(["[[Windows: Byte-Exact Artifacts]]"], index).values == (PAGE,)


def test_two_names_for_one_document_do_not_leave_the_edge_twice() -> None:
    """A duplicate edge is meaningless, and would fail the API's uniqueness rule.

    Only the duplicate resolution created is collapsed; this does not tidy an
    edge list that arrived with one.
    """

    resolution = resolve_edges(
        ["[[Calibrating a Semantic Dedup Threshold]]", "[[semantic-dedup]]"], _index()
    )

    assert resolution.values == (PAGE,)
    assert len(resolution.resolved) == 2


def test_a_link_resolving_to_an_id_already_in_the_list_does_not_duplicate_it() -> None:
    """The mixed representation a half-repaired row carries.

    Order must not matter: whichever of the two comes first, the plain id keeps
    its position and the resolved link folds into it. Leaving both would write a
    row the API's uniqueness rule then refuses to update.
    """

    for values in (
        [PAGE, "[[semantic-dedup]]"],
        ["[[semantic-dedup]]", PAGE],
    ):
        resolution = resolve_edges(values, _index())

        assert resolution.values == (PAGE,), values
        assert resolution.changed


def test_an_id_that_arrives_twice_is_left_alone() -> None:
    """Tidying an edge list that arrived duplicated is not this function's job.

    The boundary of the rule above: resolution collapses what resolution
    created, and nothing else.
    """

    resolution = resolve_edges([PAGE, PAGE], _index())

    assert resolution.values == (PAGE, PAGE), (
        "A pre-existing duplicate was collapsed. Do not accept this by "
        "updating the expected value: the rule is that resolution collapses "
        "what resolution created and nothing else, so a function that also "
        "tidies its input has quietly taken on a second job and will rewrite "
        "rows nobody asked it to touch."
    )
    assert not resolution.changed, (
        "`changed` went true for input that was left identical, which makes "
        "the repair script rewrite rows it does not need to and lose its "
        "second-run-writes-nothing property."
    )


def test_a_name_shaped_value_that_names_nothing_is_dropped() -> None:
    """`looks_like_a_name` is what makes the exporter warn, so anything it
    flags has to be something the repair can actually finish.

    A lone bracket or a run of whitespace strips to no name at all. Passing it
    through leaves a warning that never clears no matter how often the repair
    is run, which is the failure the repair exists to end.
    """

    for junk in ("[", "]", "[]", "[[]]", "   ", "[[   ]]"):
        assert looks_like_a_name(junk), junk

        resolution = resolve_edges([junk], _index(), resolve_names=True)

        assert resolution.values == (), junk
        assert resolution.dropped == (junk,), junk
        # Dropping is a rewrite, so the row reaches the planner and the
        # original list is preserved before the value leaves it.
        assert resolution.changed, junk


def test_a_value_that_names_nothing_is_still_left_alone_at_the_write_boundary() -> None:
    """Without `resolve_names` nothing is rewritten -- the caller refuses on
    `malformed` instead, which is what the import path does."""

    for junk in ("[", "[]", "   "):
        resolution = resolve_edges([junk], _index())

        assert resolution.values == (junk,), junk
        assert resolution.malformed == (junk,), junk
        assert not resolution.changed, junk


def test_order_is_preserved() -> None:
    resolution = resolve_edges([OTHER, "[[semantic-dedup]]", "[[The .env one]]"], _index())

    assert resolution.values == (OTHER, PAGE, NOTE)


def test_ids_render_as_slug_wikilinks_for_the_export() -> None:
    assert render_edges([PAGE, NOTE], _index()) == ["[[semantic-dedup]]", "[[two-loaders]]"]


def test_an_id_the_run_cannot_resolve_is_omitted_rather_than_rendered() -> None:
    """A broken wikilink is worse than an absent one (ADR 0025).

    A dangling edge is normal here -- the target may be retired, or outside the
    exported prefixes -- so this is the ordinary case, not an error case.
    """

    assert render_edges([PAGE, OTHER], _index()) == ["[[semantic-dedup]]"]


def test_an_empty_index_resolves_nothing_and_renders_nothing() -> None:
    """``render_document`` without a link index takes this path deliberately."""

    empty = LinkIndex(())

    assert render_edges([PAGE], empty) == []
    assert resolve_edges(["[[Anything]]"], empty).dropped == ("[[Anything]]",)


@pytest.mark.parametrize(
    "value",
    [
        "[[Operating the Agent Knowledge Vault]]",
        "[[operating]]",
        "Operating the Agent Knowledge Vault",
        " 9711ac5985974fcdbfe0c33aa071d390",
        "9711ac5985974fcdbfe0c33aa071d390 ",
    ],
)
def test_a_name_is_recognised_as_one(value: str) -> None:
    """ADR 0030's rule, stated as a property of names rather than of ids."""

    assert looks_like_a_name(value)


@pytest.mark.parametrize(
    "value",
    [
        PAGE,
        # Not the service's format, and deliberately still accepted: this rule
        # must not pin the id format into the wire contract.
        "note_01J8XKQ0000000000000000000",
        "01J8XKQZ7YV3M8P6R4W2N9C5FA",
        # The accepted residual. A bare slug carries neither whitespace nor a
        # bracket, so it passes -- catching it means knowing the format.
        "operating-the-agent-knowledge-vault",
    ],
)
def test_anything_that_could_be_an_id_is_left_alone(value: str) -> None:
    assert not looks_like_a_name(value)
