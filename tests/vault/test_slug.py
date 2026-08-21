"""Title-derived vault paths.

The security-relevant half is that a title cannot escape the folder the service
chose. ADR 0022's original wording forbade any path from contributor input to
``vault_path`` at all; the 2026-08-20 amendment narrows it to the folder, and
these are the tests that hold the narrowed line.
"""

import pytest

from app.vault.service import AGENT_NOTES_DIRECTORY
from app.vault.slug import SLUG_MAX_LENGTH, resolve_vault_path, slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("A patch file CRLF-mangled in transit", "a-patch-file-crlf-mangled-in-transit"),
        ("  Leading and trailing  ", "leading-and-trailing"),
        ("Load probe: pool saturation", "load-probe-pool-saturation"),
        ("Multiple---separators", "multiple-separators"),
        # str.isalnum is script-aware, so a non-ASCII title stays readable
        # instead of collapsing to "untitled".
        ("Überprüfung der Verbindung", "überprüfung-der-verbindung"),
    ],
)
def test_slugify_produces_a_readable_name(title: str, expected: str) -> None:
    assert slugify(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "../../etc/passwd",
        "Human/06 Reference/planted",
        "..",
        "/absolute",
        "back\\slash",
        "C:\\Windows\\System32",
        "nul",
    ],
)
def test_no_title_can_introduce_a_path_separator(title: str) -> None:
    """The narrowed ADR 0022 invariant, stated as a test.

    A contributor may influence the leaf name. It may not choose a folder, and
    the only way it could is by smuggling a separator through the slug.
    """

    slug = slugify(title)

    assert "/" not in slug
    assert "\\" not in slug
    assert ":" not in slug
    assert slug not in ("", ".", "..")

    path = resolve_vault_path(AGENT_NOTES_DIRECTORY, title, ())
    assert path.startswith(AGENT_NOTES_DIRECTORY)
    assert path.count("/") == AGENT_NOTES_DIRECTORY.count("/")


def test_a_title_that_slugifies_to_nothing_still_yields_a_name() -> None:
    assert slugify("!!! ???") == "untitled"
    assert slugify("") == "untitled"


def test_windows_reserved_names_are_suffixed() -> None:
    """The vault runs on Linux and is developed on Windows; the projection has
    to be writable in both."""

    assert slugify("CON") == "con-note"
    assert slugify("lpt1") == "lpt1-note"


def test_long_titles_are_truncated_without_a_trailing_hyphen() -> None:
    slug = slugify("word " * 60)

    assert len(slug) <= SLUG_MAX_LENGTH
    assert not slug.endswith("-")


def test_colliding_titles_get_a_stable_numeric_suffix() -> None:
    """Two notes may legitimately share a title -- the dedup gate scores
    meaning, not titles -- and ``vault_path`` is UNIQUE."""

    taken: set[str] = set()
    paths = []
    for _ in range(3):
        path = resolve_vault_path(AGENT_NOTES_DIRECTORY, "Shared title", taken)
        taken.add(path)
        paths.append(path)

    assert paths == [
        "Agent/notes/shared-title.md",
        "Agent/notes/shared-title-2.md",
        "Agent/notes/shared-title-3.md",
    ]


def test_a_directory_without_a_trailing_slash_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_vault_path("Agent/notes", "Anything", ())
