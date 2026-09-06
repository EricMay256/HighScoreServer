"""What the propose form does, not what its source says.

Every other console test in this suite matches strings against the rendered
page. That catches a deleted guard and nothing else -- an inverted `pristine`
assignment satisfied all of them while meaning the opposite of its name, and a
range check that reported an error without invalidating the span satisfied them
too. Both were review findings, and neither could have been a test failure.

So this module runs the page's own script against a stub DOM (see
`console_harness.js`) and drives the real handlers: type into the replacement,
change the line numbers, click Propose, and look at what came out. It is not a
browser and does not pretend to be one; it observes state transitions and the
request payload, which is exactly where these defects lived.

Skipped where `node` is absent rather than requiring it: the vault's toolchain
is Python, and a JavaScript runtime is worth using when it is there and not
worth demanding when it is not. CI images carry one.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.vault import browse_console as browse
from app.vault.console_page import API_BASE
from app.vault.templating import render


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the console behaviour harness needs it",
)

HARNESS = Path(__file__).parent / "console_harness.js"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Drive the browse console's propose form once, and read what it did."""

    page = render(
        "browse.html",
        api_base=API_BASE,
        scopes=browse.CONSOLE_SCOPES,
        console_path=browse.BROWSE_PATH,
        client_name=browse.CLIENT_NAME,
        store_prefix=browse.STORE_PREFIX,
    )
    scripts = [
        body
        for attributes, body in re.findall(
            r"<script([^>]*)>(.*?)</script>", page, re.S
        )
        # The config block is JSON, not JavaScript; the harness supplies its own.
        if "application/json" not in attributes
    ]
    script = tmp_path_factory.mktemp("console") / "browse.js"
    script.write_text("\n".join(scripts), encoding="utf-8")

    finished = subprocess.run(
        ["node", str(HARNESS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_an_edited_replacement_survives_retargeting(report: dict) -> None:
    """The operator's words are not the form's to discard.

    `pristine` was written as `value !== current.expected`, so typing marked
    the field pristine and changing the line range then overwrote what had been
    typed -- the exact loss the flag exists to prevent.
    """

    outcome = report["editedReplacementSurvivesRetarget"]

    assert outcome["replacement"] == "A sentence the operator wrote."
    # The span itself did move: this is a re-aim, not a no-op.
    assert outcome["quoted"] == "Second line, the one to reword."


def test_an_untouched_replacement_follows_the_range(report: dict) -> None:
    """Re-seeding is the useful half of the same rule."""

    assert (
        report["untouchedReplacementFollowsTheRange"]["replacement"]
        == "Second line, the one to reword."
    )


def test_restoring_the_original_text_resumes_reseeding(report: dict) -> None:
    """Pristine is a property of the current value, not a latch.

    Typing and then undoing leaves the field matching its span again, and the
    form may seed it again -- which is what "still matches" has to mean if the
    name is to be worth anything.
    """

    assert (
        report["restoringTheTextResumesReseeding"]["replacement"]
        == "Third line, untouched."
    )


def test_an_impossible_range_refuses_to_submit(report: dict) -> None:
    """Reporting an error is not the same as refusing to act on it.

    `reaim` printed "No such lines" and left `current` on the last valid span,
    so Submit stayed live and posted an edit against lines the form had stopped
    showing as chosen.
    """

    outcome = report["invalidRangeRefuses"]

    assert outcome["submitDisabled"] is True
    assert outcome["requests"] == 0, "an invalid range must post nothing"


def test_an_edit_made_while_the_range_is_invalid_survives_recovery(
    report: dict,
) -> None:
    """The replacement remains editable while no span is selected.

    An invalid range clears the current span, so the replacement handler must
    tolerate that nullable state and remember that the operator has typed.
    Restoring a valid range must not reseed over those words.
    """

    outcome = report["invalidRangeEditSurvivesRecovery"]

    assert outcome["submitEnabled"] is True
    assert outcome["replacement"] == "An edit made while the range was invalid."


def test_a_valid_range_posts_exactly_what_is_shown(report: dict) -> None:
    """And recovers: a bad range is a state to leave, not a dead end."""

    outcome = report["validRangePostsWhatIsShown"]

    assert outcome["submitEnabled"] is True
    assert outcome["url"] == "/api/v1/vault/amendment-proposals"
    assert outcome["body"]["change"] == {
        "kind": "span",
        "expected_text": "Second line, the one to reword.",
        "replacement_text": "Second line, reworded.",
        "occurrence": 1,
    }
    # The revision the page read, not the newest one.
    assert outcome["body"]["base_revision"] == 4


def test_a_partial_line_selection_is_submitted_exactly(report: dict) -> None:
    """Line inputs describe a selection but must not broaden it implicitly.

    Until the operator edits those inputs, the exact character span selected
    with the mouse remains authoritative without including surrounding text.
    """

    change = report["partialLineSelectionStaysExact"]["body"]["change"]

    assert change == {
        "kind": "span",
        "expected_text": "the one to reword",
        "replacement_text": "the phrase the operator revised",
        "occurrence": 1,
    }


def test_fractional_line_numbers_are_refused(report: dict) -> None:
    """Fractional indexes make the offset loop and `slice` disagree."""

    outcome = report["fractionalRangeRefuses"]

    assert outcome["submitDisabled"] is True
    assert outcome["requests"] == 0


def test_full_body_edit_posts_the_exact_bodies(report: dict) -> None:
    """The complete editor is another span authoring gesture.

    The old text must be the body fetched at revision 4, while the replacement
    may contain edits in distant parts of the note. The server owns conversion
    to the canonical compact diff; the browser must not reduce this to one
    locally chosen hunk.
    """

    outcome = report["fullBodyPostsExactBodies"]
    original = (
        "First line, untouched.\n"
        "Second line, the one to reword.\n"
        "Third line, untouched.\n"
    )
    replacement = (
        "First line, revised.\n"
        "Second line, the one to reword.\n"
        "Third line, also revised.\n"
    )

    assert outcome["seededBody"] == original
    assert outcome["url"] == "/api/v1/vault/amendment-proposals"
    assert outcome["body"] == {
        "target_note_id": "harness-note",
        "base_revision": 4,
        "change": {
            "kind": "span",
            "expected_text": original,
            "replacement_text": replacement,
            "occurrence": 1,
        },
        "rationale": "Revise two distant parts of the note.",
    }


def test_unchanged_full_body_is_refused_in_the_browser(report: dict) -> None:
    """Opening the editor alone must not enqueue an empty amendment."""

    assert report["unchangedFullBodyRefuses"]["requests"] == 0


def test_rendered_and_source_modes_keep_the_source_authoritative(
    report: dict,
) -> None:
    """Rendering is a view over the fetched body, never a replacement for it."""

    outcome = report["markdownBodyModes"]

    assert outcome["rawText"] == outcome["source"]
    assert outcome["parsed"] == {
        "source": outcome["source"],
        "options": {"async": False, "breaks": False, "gfm": True},
    }
    assert outcome["sanitized"]["html"] == (
        '<h1>Parsed Markdown</h1><script>alert("unsafe")</script>'
    )
    sanitizer = outcome["sanitized"]["options"]
    assert sanitizer["RETURN_DOM_FRAGMENT"] is True
    assert sanitizer["ALLOW_ARIA_ATTR"] is False
    assert sanitizer["ALLOW_DATA_ATTR"] is False
    assert "script" not in sanitizer["ALLOWED_TAGS"]
    assert "input" not in sanitizer["ALLOWED_TAGS"]
    assert "style" not in sanitizer["ALLOWED_ATTR"]
    assert outcome["insertedText"] == "Sanitized fragment"

    assert outcome["initial"] == {
        "renderedSelected": "true",
        "sourceSelected": "false",
        "renderedHidden": False,
        "sourceHidden": True,
    }
    assert outcome["afterSource"] == {
        "renderedSelected": "false",
        "sourceSelected": "true",
        "renderedHidden": True,
        "sourceHidden": False,
    }


def test_a_stale_listing_response_cannot_replace_newer_filters(report: dict) -> None:
    """Rows and their cursor must come from one navigation generation."""

    outcome = report["reversedListingsKeepNewest"]

    assert outcome["rows"] == ["New listing"]
    assert outcome["cursor"] == "new-cursor"


def test_changing_the_order_starts_a_new_walk(report: dict) -> None:
    """A cursor belongs to the order it was issued in.

    The endpoint refuses a foreign one with 422 rather than re-seating it, so
    a console that carried its cursor across a change of order would turn the
    reader's own choice into an error they did not cause -- and would do it
    only when a page had already been paged, which is the state a manual pass
    is least likely to be in.

    Nothing guards this by name. It falls out of a change of order starting a
    fresh listing, and a fresh listing requesting no cursor. That is precisely
    why it is driven rather than matched: there is no line of source to assert.
    """

    outcome = report["changingOrderStartsANewWalk"]

    assert outcome["hadCursor"] == "path-cursor", (
        "the fixture must have paged before changing order, or this proves "
        "nothing"
    )
    assert outcome["sortRequested"] == "updated"
    assert outcome["afterRequested"] is None, (
        "the old order's cursor must not travel into the new order's request"
    )
    assert outcome["cursorAfterwards"] == "updated-cursor"
    assert outcome["rows"] == ["Recent listing"], (
        "a new walk replaces the rows rather than appending to them"
    )


def test_failed_order_replacement_keeps_the_committed_folder_mode(
    report: dict,
) -> None:
    """Presentation belongs to the listing that actually produced the rows.

    A failed fresh request deliberately preserves the prior listing and its
    pagination. The select now holds the candidate order, so consulting it
    while paging the preserved listing would show folders for a non-path walk.
    """

    outcome = report["failedOrderReplacementKeepsCommittedFolderMode"]

    assert outcome["committedSort"] == "updated"
    assert outcome["folderCount"] == 0


def test_a_stale_note_response_cannot_replace_newer_navigation(report: dict) -> None:
    """The last note opened remains authoritative when responses reverse."""

    assert report["reversedNotesKeepNewest"]["note"] == "New note"


def test_failed_pagination_keeps_the_listing_retryable(report: dict) -> None:
    """A transient next-page failure must not strand accumulated results."""

    outcome = report["failedPaginationKeepsListing"]

    assert outcome["rows"] == ["Kept listing"]
    assert outcome["cursor"] == "kept-cursor"
    assert outcome["rowVisible"] is True
    assert outcome["retryEnabled"] is True


def test_a_failed_fresh_listing_keeps_the_previous_view(report: dict) -> None:
    """Replacement is committed only after the new page succeeds."""

    outcome = report["failedRefreshKeepsListing"]

    assert outcome["rows"] == ["Kept listing"]
    assert outcome["cursor"] == "kept-cursor"
    assert outcome["rowVisible"] is True
    assert outcome["loadMoreVisible"] is True


def test_preserved_pagination_uses_its_committed_query(report: dict) -> None:
    """A visible old button must never combine its cursor with new filters."""

    outcome = report["preservedPaginationUsesCommittedQuery"]
    query = parse_qs(urlparse(outcome["url"]).query)

    assert query["tag"] == ["kept"]
    assert query["after"] == ["kept-cursor"]
    assert outcome["query"] == {
        "prefix": "",
        "filters": {"tag": "kept", "facet": ""},
        # The order is part of the committed query, so an appended page keeps
        # the order its walk began in rather than reading the control now.
        "sort": "path",
    }
    assert outcome["rows"] == ["Kept listing", "Appended listing"]
    assert outcome["cursor"] == "appended-cursor"


def test_note_navigation_invalidates_pagination_in_flight(report: dict) -> None:
    """Leaving the listing prevents a late page from rewriting it behind a note."""

    outcome = report["noteNavigationInvalidatesPagination"]

    assert outcome["rows"] == ["Listing before note"]
    assert outcome["cursor"] == "before-note-cursor"
    assert outcome["note"] == "Newer navigation"


def test_concurrent_sign_in_calls_share_one_attempt(report: dict) -> None:
    """Registration and PKCE storage are one single-flight operation."""

    outcome = report["concurrentSignInIsSingleFlight"]

    assert outcome == {
        "sharedPromise": True,
        "registrations": 1,
        "verifierWrites": 1,
        "stateWrites": 1,
        "navigations": 1,
    }
