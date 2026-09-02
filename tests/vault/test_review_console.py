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
    CLIENT_NAME,
    CONSOLE_SCOPES,
    REVIEW_PATH,
    STORE_PREFIX,
    build_vault_review_routes,
)
from app.vault.templating import TEMPLATE_DIRECTORY, render


def _page() -> str:
    return render(
        "review.html",
        api_base=API_BASE,
        scopes=CONSOLE_SCOPES,
        console_path=REVIEW_PATH,
        client_name=CLIENT_NAME,
        store_prefix=STORE_PREFIX,
    )


def _session_module() -> str:
    """The shared module as written, before a page includes it."""

    return (TEMPLATE_DIRECTORY / "_console_session.js").read_text(encoding="utf-8")


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


def test_evidence_bodies_load_on_expand_not_while_rendering_the_queue() -> None:
    """Painting the queue must not spend the reviewer's `get_note` burst.

    The cases tab used to await `GET /notes/{id}` for every similar note of
    every pending case while rendering. `get_note` is quota-limited at 120/min
    with a burst of 30 per principal, so a handful of cases carrying five
    pieces of evidence each drained the burst and the remaining cases rendered
    as errors -- on the surface a reviewer meets precisely when the queue is
    long enough to matter.

    The stored `similar` already carries id, title and score, which is what
    triage needs. Only the body costs a request, and only once the reviewer
    asks for it. The amendment queue was fixed for this same shape; this pins
    that the cases tab does not drift back.
    """

    page = _page()

    loop = page.index("for (const item of evidence)")
    toggle = page.index('d.addEventListener("toggle"', loop)
    fetch = page.index('await api("/notes/"', loop)
    assert toggle < fetch, (
        "the evidence body must be fetched from the toggle handler, so that "
        "rendering the queue costs no get_note requests at all"
    )
    assert "(loads on open)" in page


def test_deciding_still_requires_every_comparison_to_have_been_read() -> None:
    """Deferring the fetch must not quietly weaken the gate it fed.

    The old gate counted bodies the page had prefetched; the new one counts
    bodies the reviewer opened, which is the same rule reached more honestly.
    What must not change is that a case is undecidable until the candidate and
    every piece of its evidence are on screen -- including evidence naming no
    note id, which can never be read and so leaves the case undecidable.
    """

    page = _page()

    assert "evidenceTotal = evidence.length;" in page, (
        "every piece of evidence counts toward the gate, including one that "
        "names no note id"
    )
    assert (
        "candidateLoaded && evidenceTotal > 0 && evidenceRead === evidenceTotal"
        in page
    )
    assert "if (!evidenceLoaded) return;" in page


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


def test_bulk_acceptance_counts_each_decision_outcome_separately() -> None:
    """`decide` returning nothing made every refusal look like a success.

    The bulk loop counted attempts, so a run where the API rejected every
    proposal still reported them all accepted. A stale proposal is settled but
    unapplied, so it must not inflate that accepted total either.
    """

    page = _page()

    assert "refused " in page
    assert "accepted++" in page and "failed++" in page
    assert "stale++" in page
    assert 'result.outcome === "accepted"' in page
    assert "check.checked = false;" in page
    assert "check.disabled = true;" in page
    assert 'check.disabled = summary.change_kind !== "metadata";' in page


def test_bulk_acceptance_uses_one_bounded_batch_request() -> None:
    """One UI action stays inside the endpoint's bounded batch contract."""

    page = _page()

    assert 'api("/amendment-proposals/batch-decisions"' in page
    assert "decisions: chosen.map" in page
    assert 'c.decide("accepted")' not in page
    assert "const maxBatchDecisions = 50;" in page
    assert "selectionLimitMessage" in page
    assert "chosen.length > maxBatchDecisions" in page


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
    # Composed from the prefix since the session module was shared, so both
    # halves are asserted: the derivation, and the value that makes it match
    # what v74 actually wrote.
    assert STORE_PREFIX == "vault.review"
    assert 'sessionStorage.getItem(CFG.storePrefix + ".client_id")' in page
    assert 'sessionStorage.getItem(CFG.storePrefix + ".refresh")' in page
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


def test_a_bulk_run_is_one_request_and_propagates_session_expiry() -> None:
    """One expired batch must repaint once, with no per-card request loop."""

    page = _page()

    assert 'api("/amendment-proposals/batch-decisions"' in page
    assert "if (err.sessionEnded) return;" in page


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
            # The window, not the `catch` line: an exemption's reason is
            # usually written inside the block it excuses.
            unguarded.append((index + 1, "\n".join(window)))

    # Exempt because none of these wrap an `api()` call, so none can ever see
    # the marker. Named by the reason rather than counted, so adding a catch
    # has to state which case it is instead of moving a number.
    exemptions = (
        "return null;",  # reading the persisted session record
        "Private mode",  # writing it
        "Signing out locally",  # revocation, best-effort by design
        "PENDING_ERROR",  # the token exchange, before a session exists
        # Renewal, which wraps `refreshTokens` rather than `api`. A network
        # failure here is not an expiry to propagate -- it is the thing that
        # must not escape, or startup never renders.
        "Settle, never reject",
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


def test_a_reopened_tab_resumes_from_the_persisted_record() -> None:
    """Persisting the token is pointless if nothing ever presents it.

    A new tab has no access token -- that lives in session storage -- but the
    refresh token on disk can mint one. `render` decides signed-in from the
    access token alone, so without an explicit resume the page showed "Sign in"
    while holding a usable credential, started a fresh authorization, and
    landed in a family with no entitlement. That is exactly the outcome
    persistence was added to prevent, so the feature did nothing at all.

    The resume must complete before the first paint, or the operator sees a
    "Sign in" flash and may click it -- which really does start a new family.
    """

    page = _page()
    lines = page.splitlines()

    assert "async function resumeSession()" in page
    assert "if (!stored || !stored.refresh) return false;" in page

    resumed_at = next(
        i for i, text in enumerate(lines) if "await resumeSession();" in text
    )
    rendered_at = next(
        i
        for i, text in enumerate(lines)
        if text.strip() == "render();" and i > resumed_at
    )
    assert resumed_at < rendered_at, (
        "the resume has to finish before the first render, not after it"
    )


def test_the_session_helpers_exist_before_the_record_is_built() -> None:
    """A `const` used earlier in the file than it is declared throws.

    `saveSession` stamps with `randomString`, and the record is built during
    module initialization. With `randomString` declared further down it sat in
    its temporal dead zone, so the legacy migration threw a ReferenceError that
    its own broad catch turned into a silent "no legacy session".

    The migration was therefore dead code that tested green, because calling it
    from a console after load is the one context where the ordering cannot
    bite. Assert the order, not the behaviour of a hand-run call.
    """

    page = _page()
    lines = page.splitlines()

    def line_of(needle: str) -> int:
        return next(i for i, text in enumerate(lines) if needle in text)

    assert line_of("const randomString =") < line_of("let SESSION = loadSession()"), (
        "randomString is declared after the record that stamps itself with it, "
        "so module initialization throws into a catch that hides the failure"
    )


def test_a_failed_renewal_still_leaves_the_operator_a_control() -> None:
    """The page starts with every control hidden, so not rendering is fatal.

    `signin`, `signout`, `refresh` and the queues all carry `hidden` in the
    initial markup; `render` is what reveals the right ones. A renewal that
    rejects -- an unreachable metadata endpoint, a dropped token request --
    escaped before `render` and left an inert page with nothing to click, which
    is worse than any renewal failure it was reporting.

    `resumeSession` therefore settles rather than rejecting, and `render` runs
    in a `finally` regardless. Both, because either alone is one refactor away
    from the same blank page.
    """

    page = _page()

    assert "Settle, never reject" in page
    assert "} finally {" in page
    lines = page.splitlines()
    finally_at = next(i for i, text in enumerate(lines) if "} finally {" in text)
    assert "render();" in lines[finally_at + 1], (
        "rendering must be the finally body; a renewal failure has to leave a "
        "usable page behind it"
    )
    assert "Could not renew this session" in page, (
        "a blank recovery is only marginally better than a blank page -- say "
        "what failed and that a reload retries it"
    )


def test_the_startup_sequence_is_callable_on_its_own() -> None:
    """Named so it can be executed by a test rather than approximated by one.

    Every defect found in this file hid in the gap between a function and the
    moment it runs: a migration that worked when called by hand and threw at
    initialization, a resume that existed and was never invoked, a stub so
    broad it answered the metadata request. An anonymous startup body cannot be
    driven, only guessed at.
    """

    page = _page()

    assert "async function boot()" in page
    assert "\nboot();" in page, "the named boot must actually be invoked"


def test_the_header_prefers_the_label_and_falls_back_to_the_credential_id() -> None:
    """A name when there is one, the id when there is not.

    The fallback is the whole header before ADR 0040, so losing it would be a
    regression to a blank header rather than to an unreadable one -- and an
    unlabelled authorization is the ordinary state, not a failure.
    """

    page = _page()

    assert "if (IDENTITY && IDENTITY.label) return IDENTITY.label;" in page
    assert 'return "credential " + (credentialId() || "?");' in page


def test_the_label_reaches_the_page_as_text_and_never_as_markup() -> None:
    """Unverified operator text, rendered by assignment to `textContent`.

    ADR 0040 accepts operator text into the database on the understanding that
    it is displayed rather than interpreted. That understanding is code, here.
    """

    page = _page()

    assert 'function paintWho() { $("who").textContent = whoText(); }' in page
    assert ".innerHTML" not in page, (
        "the page builds its DOM through textContent; an assignment to "
        "innerHTML anywhere is a route for operator text to become markup"
    )


def test_the_identity_request_cannot_cost_the_operator_their_queue() -> None:
    """Awaited, it would put a name in front of the work.

    `loadAll` is the queue. A header lookup that threw inside its `try` would
    be reported as a failure to load proposals, and one that were awaited would
    delay them behind a round trip that decorates the page. It is fired without
    `await` and swallows its own failure, leaving the credential-id fallback.
    """

    page = _page()
    lines = page.splitlines()
    load_all_at = next(
        i for i, text in enumerate(lines) if "async function loadAll()" in text
    )
    body = chr(10).join(lines[load_all_at : load_all_at + 5])

    assert "refreshIdentity();" in body
    assert "await refreshIdentity()" not in page
    assert "async function refreshIdentity()" in page


def test_a_signed_out_header_says_nothing() -> None:
    """Sign-out clears the label with the token it described."""

    page = _page()

    assert 'if (!TOKEN) return "";' in page
    assert page.count("IDENTITY = null;") >= 2, (
        "both ends of a session -- expiry and sign-out -- have to drop the "
        "identity, or a signed-out header keeps naming the family that left"
    )


def test_sign_out_sends_the_field_the_revocation_endpoint_requires() -> None:
    """The console is a public client, and still has to send `client_secret`.

    The SDK's `RevocationRequest` declares it with no default, so a form
    omitting it is refused with 400 before any token is loaded. That is what
    every sign-out did until this was fixed: the local session ended, the
    family did not, and its refresh token stayed valid for thirty days.

    `test_the_console_sign_out_form_is_accepted_by_the_revocation_endpoint`
    holds the other half of this -- that the endpoint accepts exactly this
    form.
    """

    page = _page()

    assert 'client_id: client, client_secret: ""' in page
    assert "Vault sign-out could not revoke this session:" in page, (
        "a revocation that fails must say so; this one was silent for months"
    )


def test_a_refresh_after_the_entitlement_lands_shows_the_queue() -> None:
    """The sequence the console itself instructs, which used to end blank.

    A 403 hides the app panel and prints the `grant-oauth` command. Running it
    and clicking Refresh calls `loadAll` directly -- never `render` -- so the
    queue loaded into a panel that was still hidden, and the operator saw an
    empty page with no message. `render` unhiding it was not enough, because
    nothing re-rendered.
    """

    page = _page()
    lines = page.splitlines()
    start = next(i for i, line in enumerate(lines) if "async function loadAll()" in line)
    body = lines[start : start + 14]

    assert any('$("app").classList.remove("hidden")' in line for line in body), (
        "loadAll must reveal the panel it is about to fill; the 403 path hides "
        "it and Refresh does not re-render"
    )
