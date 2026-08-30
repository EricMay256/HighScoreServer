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
    assert "evidenceLoaded" in page and "if (!evidenceLoaded) return;" in page
    assert "if (!loaded) return false;" in page
    # A card whose preview never arrived starts undecidable. It becomes
    # decidable by loading the evidence, never by leaving the buttons live --
    # see test_a_dropped_preview_can_still_be_loaded for the recovery.
    assert "if (!loaded) {\n    check.disabled = true;" in page


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


def test_the_persisted_session_is_bounded_by_the_refresh_lifetime() -> None:
    """The registration may outlive the tab, but never its refresh token.

    The entitlement is keyed to the OAuth *family*, so losing the session means
    re-running `grant-oauth`. Session-scoping the refresh token made that a
    chore on every tab close rather than the monthly one the thirty-day refresh
    lifetime implies, so the record persists.

    What persistence must not do is recreate the stranding bug.
    `prune_vault_oauth` deletes a registration once its refresh token expires,
    and the authorization server answers a deleted client_id with a direct 400
    and no redirect (mcp/server/auth/handlers/authorize.py,
    `attempt_load_client=False`) -- so no error ever comes back to clean up
    from. The record is therefore discarded on our own schedule, by comparing
    `obtained` against the refresh lifetime, rather than by waiting for a
    failure that cannot arrive.

    The client id and refresh token are one record because they are only valid
    together: a refresh presents both, and an id from one authorization cannot
    renew a token from another. Storing them apart is how they drift.
    """

    page = _page()

    assert "REFRESH_TTL_MS" in page
    assert "Date.now() - saved.obtained > REFRESH_TTL_MS" in page, (
        "the persisted record must expire on its own schedule; nothing will "
        "tell the browser its registration was pruned"
    )
    assert "STORE.session" in page
    assert "if (!saved.client || !saved.obtained) return null;" in page, (
        "a half-written record should be discarded rather than half-used"
    )
    # The access token stays session-scoped: it is renewable from the record.
    assert "sessionStorage.getItem(STORE.token)" in page


def test_refresh_rotation_is_serialized_across_tabs() -> None:
    """Two tabs sharing one refresh token is a family-destroying race.

    Every tab copies the refresh token into memory. If two hold R1 and one
    rotates it to R2, the other presents a *consumed* token -- which
    `VaultOAuthProvider.load_refresh_token` treats as a captured credential and
    answers by burning the whole family, every credential in the chain. That is
    right for theft and catastrophic for a second tab, because the
    `vault:review` entitlement dies with the family and must be granted again
    by hand.

    A Web Lock alone is not the fix: the losing tab must *re-read* storage
    inside the lock, or it serializes presenting the stale token. Both halves
    are asserted.
    """

    page = _page()

    assert "navigator.locks" in page
    assert "withRefreshLock" in page
    assert "const current = loadSession();" in page, (
        "the lock holder must re-read the record; holding a lock while "
        "presenting a token another tab already consumed changes nothing"
    )
    # Presenting the in-memory copy is the bug; the re-read one is the fix.
    assert "refresh_token: presented," in page


def test_the_legacy_session_storage_format_is_migrated() -> None:
    """The deployed console wrote a different shape, and it is in use.

    Release v74 shipped the console storing `vault.review.client_id` and
    `vault.review.refresh` in session storage. Reading only the new record
    would find no refresh token on the first 401 after rollout, end the
    session, and re-authorize into a family with no entitlement -- so every
    live reviewer would have to run `grant-oauth` again, which is the chore
    the persisted record exists to remove.

    The legacy keys are cleared only after the new record is written, so an
    interrupted upgrade repeats instead of losing the token.
    """

    page = _page()

    assert "migrateLegacySession" in page
    assert 'sessionStorage.getItem("vault.review.client_id")' in page
    assert 'sessionStorage.getItem("vault.review.refresh")' in page
    assert "loadSession() || migrateLegacySession() || {}" in page, (
        "migration must run only when no new record exists, or it would "
        "overwrite a current session with a stale one"
    )
    assert "if (localStorage.getItem(STORE.session)) {" in page, (
        "the legacy keys must not be removed before the new record is saved"
    )


def test_a_dropped_preview_can_still_be_loaded() -> None:
    """A truncated queue must not make a proposal unreviewable.

    The byte budget drops previews from the tail, and the API contract says
    they can be fetched individually. A card that says so without offering the
    fetch is worse than the N+1 it replaced: the old console loaded every
    detail, so nothing was ever unreachable.
    """

    page = _page()

    assert "Load preview" in page
    assert "dropped to fit the response " in page
    assert "accept.disabled = reject.disabled = false;" in page, (
        "loading the preview has to enable the decision it was blocking"
    )


def test_the_session_ended_marker_is_actually_consumed() -> None:
    """Asserting the marker exists is not asserting anything uses it.

    `endSession` repaints and explains itself, then `api` throws a marked
    error. Without a caller checking the mark, `loadAll` painted a second,
    vaguer message underneath the sign-in prompt. The previous test for this
    checked only that the string appeared, which it did while the bug was live.
    """

    page = _page()

    assert "if (err.sessionEnded) return;" in page, "loadAll does not consume the marker"
    assert "if (err.sessionEnded) return false;" in page, (
        "a card's decision handler does not consume the marker, so it would "
        "annotate a card that endSession has already torn down"
    )


def test_a_bulk_run_stops_when_the_session_ends() -> None:
    """Otherwise every remaining card 401s, ends the session again, repaints."""

    page = _page()

    assert "if (!TOKEN) break;" in page


def test_every_catch_around_an_api_call_propagates_session_expiry() -> None:
    """Structural, because a marker is only useful where it is not swallowed.

    `loadAll` consumed the marker, but the nested detail handlers caught it
    first and rendered it as an ordinary load failure -- so the queue loop kept
    going and every remaining card issued another unauthorized request, up to
    two hundred of them, against a page `endSession` had already repainted.

    Asserted across every catch block rather than the four that were wrong,
    because the next one written will be wrong the same way. Two are exempt and
    named below: neither calls `api()`, so neither can ever see the marker.
    """

    page = _page()
    lines = page.splitlines()
    unguarded = []
    for index, line in enumerate(lines):
        if "catch (err)" not in line:
            continue
        window = lines[index : index + 8]
        if not any("sessionEnded" in following for following in window):
            unguarded.append((index + 1, line.strip()[:70]))

    # Exempt because none of these wrap an `api()` call, so none can ever see
    # the marker. Named by the reason rather than counted, so adding a catch
    # has to state which case it is instead of moving a number.
    exemptions = (
        "return null;",  # reading the persisted session record
        "Private mode",  # writing it
        "Signing out locally",  # revocation, best-effort by design
        "PENDING_ERROR",  # the token exchange, before a session exists
    )
    unexplained = [
        entry
        for entry in unguarded
        if not any(reason in entry[1] for reason in exemptions)
    ]

    assert not unexplained, (
        "A catch block around an api() call does not propagate session expiry: "
        f"{unexplained}. Add `if (err.sessionEnded) throw err;` ahead of the "
        "ordinary failure rendering. Swallowing it lets the caller carry on "
        "issuing requests against a session that has already ended, and hides "
        "the expiry from the handler that repaints the sign-in controls. If "
        "the block genuinely cannot see an api() error, add it to `exemptions` "
        "with the reason."
    )


def test_a_revoked_access_token_does_not_end_a_live_session() -> None:
    """Rotation revokes the token it replaces, so one 401 proves nothing.

    With two tabs open, this tab's freshly-minted access token can be revoked
    again by the other tab's next rotation. Giving up after a single retry
    read that as "the session is over" and called `endSession`, which deleted
    the shared record -- taking the other tab's still-valid refresh token with
    it and costing the entitlement.

    The condition for giving up is therefore that a *refresh* failed, not that
    a retry was spent. Bounded so a genuinely dead session still terminates.
    """

    page = _page()

    assert "MAX_REFRESH_RETRIES" in page
    assert "tries < MAX_REFRESH_RETRIES && await refreshTokens()" in page, (
        "the retry must be gated on a refresh succeeding; counting attempts "
        "alone cannot distinguish a dead session from a busy one"
    )


def test_ending_a_session_spares_a_record_another_tab_advanced() -> None:
    """Local sign-out must not be a global one.

    `refreshTokens` clears the stored token before presenting it, so a record
    whose refresh is null is spent and dead for everyone -- clearing it is
    right. A record carrying a usable token written by a different tab is the
    opposite: deleting it ends a session that was alive.

    Sign-out passes `force`, because there the family really is revoked for
    every tab.
    """

    page = _page()

    assert "stored && stored.refresh && SESSION.stamp" in page, (
        "the guard must require a usable token; a spent record should still "
        "be cleared"
    )
    assert "stored.stamp !== SESSION.stamp" in page
    assert "endSession(null, true)" in page, "sign-out should force the clear"
    assert "next.stamp = randomString();" in page
