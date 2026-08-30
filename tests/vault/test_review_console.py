"""The operator's review console: served by the vault, scoped like any client.

The console is the only human surface in an agent-facing service, and the
security properties that make that acceptable are not visible in the page --
they are in what it asks for and what it refuses to become. These tests pin
those, not the markup.
"""

import pytest

from app.vault.constants import (
    OAUTH_BASELINE_SCOPES,
    OAUTH_OPERATOR_ENTITLEMENT_SCOPES,
)
from app.vault.review_console import (
    API_BASE,
    CONSOLE_SCOPES,
    REVIEW_PATH,
    build_vault_review_routes,
)
from app.vault.templating import render


def _page() -> str:
    return render(
        "review.html",
        api_base=API_BASE,
        scopes=CONSOLE_SCOPES,
        review_path=REVIEW_PATH,
    )


def test_the_console_requests_read_alone() -> None:
    """The scope request and the separation-of-duties rule are one decision.

    `vault:review` may be granted only to a family holding `vault:read` alone,
    so a console that also asked for `vault:write` or `vault:propose` would
    make itself permanently ineligible for the entitlement it exists to use --
    and would fail at the grant, long after the code looked fine.

    If this fails because someone widened CONSOLE_SCOPES: the console does not
    need the extra scope. It reads queues and posts decisions, and deciding is
    authorized by the entitlement, not by the baseline.
    """

    assert CONSOLE_SCOPES == "vault:read"


def test_the_console_never_asks_for_a_privileged_scope() -> None:
    """Stronger than the equality above, and states the reason.

    OAuth caps requests at the baseline, so asking for `vault:review` would not
    escalate -- it would be silently dropped and the console would look broken
    for a reason no error explains. Privileged scopes arrive by operator
    entitlement or not at all.
    """

    requested = set(CONSOLE_SCOPES.split())
    assert not requested & set(OAUTH_OPERATOR_ENTITLEMENT_SCOPES)
    assert requested <= set(OAUTH_BASELINE_SCOPES)


def test_the_page_tells_an_operator_how_to_grant_the_entitlement() -> None:
    """A 403 here is the expected first run, not a failure state.

    Signing in yields `vault:read`, so the first thing a new console can do is
    be refused. Without the command in front of them an operator has to find
    the separation-of-duties rule in the runbook to discover that the refusal
    is by design.
    """

    page = _page()
    assert "grant-oauth" in page
    assert "vault:review" in page
    assert "issue_vault_credential" in page


def test_the_page_warns_that_rejecting_a_case_deletes() -> None:
    """The two queues' decisions are not symmetric and the page must say so.

    Rejecting an amendment proposal discards an inert suggestion. Rejecting a
    near-duplicate case DELETES the candidate note. Both buttons are labelled
    "reject" in the API, and a console that rendered them identically would be
    inviting the mistake.
    """

    page = _page()
    assert "deletes the note" in page.lower()
    assert "cannot be undone" in page


def test_the_console_loads_no_third_party_assets() -> None:
    """Self-contained on purpose: a review surface that fetches a script from
    a CDN lets that CDN decide what an operator approves."""

    page = _page()
    for marker in ("http://", "https://", "//cdn", "integrity="):
        assert marker not in page, f"the console references an external asset: {marker}"


def test_the_route_is_registered_at_the_documented_path() -> None:
    routes = build_vault_review_routes()

    assert [route.path for route in routes] == [REVIEW_PATH]
    assert routes[0].methods is not None and "GET" in routes[0].methods


@pytest.mark.parametrize(
    "header",
    ["X-Frame-Options", "Content-Security-Policy", "Referrer-Policy", "Cache-Control"],
)
def test_the_console_sets_its_protective_headers(header: str) -> None:
    """Asserted on the endpoint rather than trusting the template.

    A framed review console is a decision the operator did not intend to make,
    which is the same reasoning the consent screen already applies.
    """

    import asyncio

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": REVIEW_PATH,
        "headers": [],
        "query_string": b"",
    }
    response = asyncio.run(build_vault_review_routes()[0].endpoint(Request(scope)))

    assert header in response.headers
    assert response.status_code == 200


def test_the_console_reads_the_field_names_the_api_actually_returns() -> None:
    """The queue's fields were guessed once, and guessing is invisible in a UI.

    `VaultReviewCaseSummary` carries `candidate_note_id`, `reason` and
    `similar`; the console read `candidate_id` and `similarity`, which are not
    fields. Nothing failed loudly -- undefined simply renders as a fallback --
    so the console looked like it worked while showing the operator nothing.
    """

    page = _page()

    assert "candidate_note_id" in page
    assert "candidate_id" not in page.replace("candidate_note_id", "")
    assert "similarity" not in page
    assert "review_case.similar" in page or "summary.similar" in page


def test_a_duplicate_case_shows_what_it_allegedly_duplicates() -> None:
    """The question is whether one note duplicates another, so both must show.

    Rendering only the candidate asks the reviewer to answer "is this a
    duplicate?" without the other side of the comparison -- and the wrong
    answer deletes a note permanently.
    """

    page = _page()

    assert "/notes/" in page, "the console never fetches the notes being compared against"
    assert "may duplicate" in page
    assert "scored " in page, "similarity scores are recorded evidence and should be shown"


def test_decisions_are_withheld_until_the_evidence_loads() -> None:
    """A failed detail fetch is not cosmetic on a surface that deletes.

    Both queues previously left their buttons live when the detail could not
    be loaded, so a reviewer could accept a proposal whose change was never
    displayed, or delete a note whose comparison never loaded.
    """

    page = _page()

    assert "Deciding is disabled until" in page
    assert "deciding is disabled until it can be shown" in page
    assert "evidenceLoaded" in page and "if (!evidenceLoaded) return;" in page
    assert "if (!loaded) return false;" in page


def test_the_console_keeps_and_rotates_its_refresh_token() -> None:
    """Discarding it silently breaks the documented "grant it once" workflow.

    The access token lasts an hour. Without a refresh, expiry sends the
    operator back through authorization -- and a new authorization creates a
    new family that inherits no privileged scopes, so `vault:review` would have
    to be re-granted hourly while abandoned families accumulated.
    """

    page = _page()

    assert "refresh_token" in page
    assert "grant_type: \"refresh_token\"" in page
    assert "revocation_endpoint" in page, "signing out should retire the family, not abandon it"


def test_bulk_acceptance_counts_refusals_separately() -> None:
    """`decide` returning nothing made every refusal look like a success.

    The bulk loop counted attempts, so a run where the API rejected every
    proposal still reported them all accepted -- and the operator would have
    had no reason to look at the cards still sitting in the queue.
    """

    page = _page()

    assert "refused " in page
    assert "accepted++" in page and "failed++" in page


def test_an_ended_session_repaints_the_sign_in_controls() -> None:
    """Clearing the token without repainting strands the operator.

    The page stayed visually signed in -- queues up, Sign in hidden -- while
    the message said to sign in again, with no control to do it. That is the
    ordinary end of a thirty-day refresh token, not an edge case, so the
    recovery has to be reachable rather than merely described.
    """

    page = _page()

    assert "function endSession" in page
    assert "endSession(" in page.replace("function endSession(", "")
    assert "sessionEnded" in page, (
        "the session-ended error should be distinguishable, so callers do not "
        "paint a second message over the one endSession already showed"
    )


def test_a_pruned_client_registration_does_not_strand_the_browser() -> None:
    """Signing out is what makes our own registration eligible for deletion.

    `prune_vault_oauth` removes a stale registration, and a registration is
    never stale while it holds a live refresh token -- so revoking on sign-out
    is precisely what allows ours to be pruned. A browser that cached the
    deleted client id would reuse it forever, with no path back: the id is
    cached indefinitely and every authorization repeats it.

    Re-registering costs one row. Being unable to sign in costs the console.
    """

    page = _page()

    assert "invalid_client" in page, "a deleted registration is never detected"
    assert page.count("localStorage.removeItem(STORE.client)") >= 3, (
        "the cached registration should be dropped on sign-out, on a failed "
        "token exchange, and on an authorization error -- each is a path where "
        "the cached id is the thing most likely to be wrong"
    )


def test_a_bulk_run_stops_when_the_session_ends() -> None:
    """Otherwise every remaining card 401s, ends the session again, repaints."""

    page = _page()

    assert "if (!TOKEN) break;" in page
