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


def test_a_stale_listing_response_cannot_replace_newer_filters(report: dict) -> None:
    """Rows and their cursor must come from one navigation generation."""

    outcome = report["reversedListingsKeepNewest"]

    assert outcome["rows"] == ["New listing"]
    assert outcome["cursor"] == "new-cursor"


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
