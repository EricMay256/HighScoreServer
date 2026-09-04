"""One stylesheet behind two consoles.

The cost of copying it is not that the pages drift apart visually -- that shows
up the moment anyone looks. It is a token changed in one file and not the
other, which reads as a rendering bug in the page nobody edited.
"""

import pytest

from app.vault import browse_console as browse
from app.vault import review_console as review
from app.vault.templating import TEMPLATE_DIRECTORY, render


VAULT_TEMPLATES = ("landing.html", "review.html", "browse.html")


def _template(name: str) -> str:
    return (TEMPLATE_DIRECTORY / name).read_text(encoding="utf-8")


def _shared() -> str:
    return _template("_console_style.css")


def _pages() -> dict[str, str]:
    return {
        "landing.html": render("landing.html"),
        "review.html": render(
            "review.html",
            api_base=review.API_BASE,
            scopes=review.CONSOLE_SCOPES,
            console_path=review.REVIEW_PATH,
            client_name=review.CLIENT_NAME,
            store_prefix=review.STORE_PREFIX,
        ),
        "browse.html": render(
            "browse.html",
            api_base=browse.API_BASE,
            scopes=browse.CONSOLE_SCOPES,
            console_path=browse.BROWSE_PATH,
            client_name=browse.CLIENT_NAME,
            store_prefix=browse.STORE_PREFIX,
        ),
    }


@pytest.mark.parametrize("template", VAULT_TEMPLATES)
def test_every_console_includes_the_shared_stylesheet(template: str) -> None:
    assert '{% include "_console_style.css" %}' in _template(template)


@pytest.mark.parametrize("template", VAULT_TEMPLATES)
def test_no_console_defines_the_palette_itself(template: str) -> None:
    """The tokens live in one file, so changing one changes both consoles.

    A page redefining `--ink` or `--panel` is how two consoles start looking
    like two products, and it would do so silently: both pages keep rendering.
    """

    source = _template(template)

    for token in ("--bg:", "--panel:", "--ink:", "--line:", "--chip:"):
        assert token not in source, (
            f"{template} defines {token} itself; the palette belongs to "
            "_console_style.css"
        )


@pytest.mark.parametrize("template", VAULT_TEMPLATES)
def test_the_shared_rules_reach_the_rendered_page(template: str) -> None:
    """Included, not merely referenced."""

    page = _pages()[template]

    assert "--ink: #1a1a19" in page
    assert "prefers-color-scheme: dark" in page
    assert ".chip, .kind {" in page


def test_every_console_leaves_room_below_its_last_line() -> None:
    """A page that ends flush with the viewport looks like a truncated one.

    Nothing distinguishes "this is the last line" from "the rest did not fit",
    so a reader scrolls to find out and a client rendering the page cannot tell
    at all. Blank space below the content answers that before it is asked,
    which is why the bottom padding is much deeper than the other three sides
    rather than uniform.
    """

    shared = _shared()

    rule = next(
        line for line in shared.splitlines() if line.startswith("main {")
    )
    padding = rule.split("padding:")[1].split(";")[0].split()

    assert len(padding) == 3, (
        "main's padding must name a bottom of its own, not one shorthand value"
    )
    bottom = float(padding[2].removesuffix("rem"))
    top = float(padding[0].removesuffix("rem"))
    assert bottom >= 4 and bottom > top * 2, (
        f"the page ends {padding[2]} above the fold, which reads as truncation"
    )


def test_the_stylesheet_leaves_checkboxes_to_the_browser() -> None:
    """The one rule here that could break a control rather than restyle it.

    A bare `input` selector applies padding, a border and a panel background to
    the reviewer's bulk-select checkboxes. Scoped by type, it reaches the text
    fields it was written for and nothing else.
    """

    shared = _shared()

    assert "input:not([type])" in shared
    for line in shared.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("input {"), (
            "an unscoped `input` rule also restyles checkboxes and radios"
        )


def test_each_console_keeps_only_what_is_its_own() -> None:
    """The split is by ownership, not by convenience.

    Reviewing has tabs, diff colouring and a bulk bar; browsing has breadcrumbs
    and a note body. Neither belongs in a file the other includes, and a rule
    that appears in both pages' own blocks is one that should have moved.
    """

    review_source = _template("review.html")
    browse_source = _template("browse.html")

    assert ".bulkbar" in review_source and ".bulkbar" not in browse_source
    assert ".diff-add" in review_source and ".diff-add" not in browse_source
    assert ".crumbs" in browse_source and ".crumbs" not in review_source
    assert ".filters" in browse_source and ".filters" not in review_source
