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
