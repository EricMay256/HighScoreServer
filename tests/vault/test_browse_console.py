"""The browse console: a second page holding a second credential.

What is worth pinning here is mostly what makes it *separate*. Two consoles in
one browser that share a storage namespace share a session record and present
each other's refresh tokens, which the authorization server reads as a captured
credential and answers by burning the family. The reviewer's scope set and this
one are also kept apart on purpose: `vault:review` may be granted only to a
family holding `vault:read` alone, so a console that could do both would be a
console that could do neither properly.
"""

import pytest
from starlette.routing import Route

from app.vault import browse_console as browse
from app.vault import review_console as review
from app.vault.console_page import CONSOLE_HEADERS, console_page
from app.vault.constants import (
    OAUTH_BASELINE_SCOPES,
    OAUTH_OPERATOR_ENTITLEMENT_SCOPES,
)
from app.vault.templating import render


def _page() -> str:
    return render(
        "browse.html",
        api_base=browse.API_BASE,
        scopes=browse.CONSOLE_SCOPES,
        console_path=browse.BROWSE_PATH,
        client_name=browse.CLIENT_NAME,
        store_prefix=browse.STORE_PREFIX,
    )


def test_the_browse_console_asks_only_for_baseline_scopes() -> None:
    """Which is the whole reason it needs no operator grant.

    A console asking for anything above the baseline would be a console that
    cannot sign in without somebody running `grant-oauth` first -- and the
    scopes above the baseline are exactly the ones an operator must decide
    deliberately.
    """

    requested = set(browse.CONSOLE_SCOPES.split())

    assert requested <= set(OAUTH_BASELINE_SCOPES)
    assert requested.isdisjoint(OAUTH_OPERATOR_ENTITLEMENT_SCOPES)


def test_the_browse_console_holds_read_and_propose() -> None:
    """Propose is requested before anything here uses it.

    Consent fixes a family's `authorized_scopes`, so adding the scope when the
    inline editor lands would mean a second authorization and a second family
    for the same page. Asking once costs nothing: proposing queues a
    suggestion, it does not change a note.
    """

    assert browse.CONSOLE_SCOPES.split() == ["vault:read", "vault:propose"]


def test_the_browse_console_never_asks_for_review() -> None:
    """The separation ADR 0021 draws, asserted rather than assumed.

    `vault:review` may be granted only to a family holding `vault:read` alone.
    A browse console requesting it would be ineligible for it, and one that
    somehow received it would be a page that authors and applies its own
    changes.
    """

    assert "vault:review" not in browse.CONSOLE_SCOPES


def test_the_two_consoles_keep_separate_storage_namespaces() -> None:
    """The defect this would be: two pages, one session record.

    Both write `<prefix>.session` and `<prefix>.token` and take a lock named
    `<prefix>.refresh`. Sharing a prefix means each rotates the other's refresh
    token, and a presentation of a consumed token is what the server treats as
    theft -- so the failure is not a muddle, it is both consoles losing their
    families at once.
    """

    assert browse.STORE_PREFIX != review.STORE_PREFIX


def test_the_two_consoles_live_at_different_paths() -> None:
    """Also their OAuth redirect URIs, so a callback cannot land on the wrong
    page holding the wrong console's verifier."""

    assert browse.BROWSE_PATH != review.REVIEW_PATH


def test_the_two_consoles_name_themselves_differently() -> None:
    """Unverified text, and still worth keeping distinct: it is what the
    consent screen shows an operator approving one of them."""

    assert browse.CLIENT_NAME != review.CLIENT_NAME


def test_the_route_is_registered_at_the_documented_path() -> None:
    routes = browse.build_vault_browse_routes()

    assert [route.path for route in routes if isinstance(route, Route)] == [
        browse.BROWSE_PATH
    ]
    assert routes[0].methods == {"GET", "HEAD"}


@pytest.mark.parametrize(
    "header",
    [
        "X-Frame-Options",
        "Content-Security-Policy",
        "Referrer-Policy",
        "Cache-Control",
    ],
)
def test_the_console_sets_its_protective_headers(header: str) -> None:
    """Served from the shared helper, so both consoles carry the same set.

    A second copy of this policy is a policy that drifts, and it drifts
    silently: the weaker page keeps working.
    """

    response = console_page(
        "browse.html",
        console_path=browse.BROWSE_PATH,
        scopes=browse.CONSOLE_SCOPES,
        client_name=browse.CLIENT_NAME,
        store_prefix=browse.STORE_PREFIX,
    )

    assert response.headers[header] == CONSOLE_HEADERS[header]


def test_the_console_loads_no_third_party_assets() -> None:
    """Its own CSP forbids them, so a reference would be a broken page."""

    page = _page()

    assert "http://" not in page.replace("http://localhost", "")
    assert "https://" not in page
    assert "cdn" not in page.lower()


def test_the_page_reads_the_field_names_the_listing_returns() -> None:
    """A console reading `notes[].path` would render an empty column and no
    error. The field names are a contract; this asserts the page uses the ones
    `VaultNoteSummary` publishes."""

    page = _page()

    for field in ("note_id", "vault_path", "doc_status", "summary", "next_cursor"):
        assert field in page


def test_the_body_is_rendered_as_text_and_never_as_markup() -> None:
    """Note bodies are written by agents, and markdown rendering in a page that
    cannot fetch a markdown library means hand-rolling one -- which is how
    untrusted text becomes markup. A `pre` block is the honest form."""

    page = _page()

    assert ".innerHTML" not in page
    assert 'el("pre", "body", detail.body)' in page


def test_the_console_includes_the_session_module_rather_than_its_own_copy() -> None:
    """The point of the extraction, from the second console's side."""

    template = (
        browse.__file__.replace("browse_console.py", "templates/browse.html")
    )
    with open(template, encoding="utf-8") as handle:
        source = handle.read()

    assert '{% include "_console_session.js" %}' in source
    for owned_by_the_module in (
        "async function refreshTokens()",
        "async function withRefreshLock(",
        "async function boot()",
    ):
        assert owned_by_the_module not in source
        assert owned_by_the_module in _page()


def test_the_page_supplies_what_the_session_module_expects() -> None:
    page = _page()

    assert "function render()" in page
    assert 'id="messages"' in page
    assert 'id="who"' in page
    assert "\nboot();" in page


# ------------------------------------------------ proposing an edit ----


def test_the_span_is_sliced_from_the_body_not_from_the_selection() -> None:
    """The correctness argument for inline editing, pinned as code.

    `expected_text` has to be the stored text byte for byte or the server
    refuses it, and a stringified selection is a rendering of that text rather
    than the text -- a `pre-wrap` block wraps long lines, and what a browser
    returns at a soft wrap has varied between them. Offsets into the body the
    server sent cannot disagree with what was stored.
    """

    page = _page()

    assert "NOTE.body.slice(range.startOffset, range.endOffset)" in page
    assert "selection.toString()" not in page


def test_the_console_resolves_the_occurrence_rather_than_being_refused() -> None:
    """The server refuses an ambiguous span, correctly. Here the ambiguity is
    already resolved: the operator pointed at one instance, and the offset says
    which. Counting every starting offset matches how the server counts."""

    page = _page()

    assert "function occurrenceOf(body, expected, start)" in page
    assert "occurrence: occurrenceOf(" in page


def test_the_proposal_carries_the_revision_the_page_read() -> None:
    """Not the newest revision, the one on screen. A span resolved against a
    body nobody saw is an edit to something else."""

    page = _page()

    assert "base_revision: NOTE.content_revision" in page


def test_the_console_sends_a_span_and_never_a_diff() -> None:
    """Generating a unified diff in the browser would need a diff
    implementation the page cannot fetch under its own CSP, which is the reason
    the kind exists over HTTP at all (ADR 0039)."""

    page = _page()

    assert 'kind: "span"' in page
    assert "body_diff" not in page


def test_a_selection_outside_the_note_body_is_refused() -> None:
    """Not clamped to the body: a selection over the title or the metadata is
    not an edit to the note text, and silently editing something adjacent is
    worse than declining."""

    page = _page()

    assert "range.startContainer !== text || range.endContainer !== text" in page


def test_the_propose_control_reads_the_scope_it_needs() -> None:
    """From `/authorization`, so a family authorized before this console asked
    for `vault:propose` says why the button is inert instead of discovering it
    at submit time with a 403."""

    page = _page()

    assert 'IDENTITY.scopes.includes("vault:propose")' in page


def test_a_wiki_page_does_not_offer_an_edit_it_cannot_take() -> None:
    """`Agent/wiki/` is readable, so pages list and open like notes.

    `VaultAmendmentService.propose` accepts note targets only, and answers
    anything else with 404 "Note not found" -- which, at the end of a filled-in
    form, reads as a bug in the console rather than as the rule it is. A page
    restates the notes it was compiled from, and the amendment belongs to
    those.
    """

    page = _page()

    assert 'if (detail.kind !== "note")' in page
    assert "Only notes take amendments." in page


def test_a_refused_edit_says_why_on_the_page() -> None:
    """A disabled control explains itself to a mouse and to nobody else."""

    page = _page()

    assert 'if (refusal) panel.appendChild(el("div", "small muted", refusal));' in page


def test_the_refusal_is_rechecked_when_the_form_opens() -> None:
    """The button is built once per note; `IDENTITY` can arrive after it."""

    page = _page()
    lines = page.splitlines()
    start = next(i for i, line in enumerate(lines) if "function startPropose()" in line)

    assert any("proposeRefusal(NOTE)" in line for line in lines[start : start + 14])


def test_the_form_is_built_through_text_content() -> None:
    """It quotes note text back at the operator, which is agent-written."""

    page = _page()

    assert ".innerHTML" not in page
    assert 'el("pre", "excerpt", current.expected)' in page


def test_opening_a_note_fetches_before_it_hides_the_listing() -> None:
    """A failed fetch used to leave a blank panel with no way back.

    The listing was hidden first, so a 404 or a dropped connection emptied the
    page and rejected into nothing -- the Back control is built after the
    await, so it did not exist yet.
    """

    page = _page()
    lines = page.splitlines()
    start = next(i for i, line in enumerate(lines) if "async function openNote(" in line)
    body = lines[start : start + 20]

    fetch_at = next(i for i, line in enumerate(body) if 'await api("/notes/"' in line)
    hide_at = next(i for i, line in enumerate(body) if '$("listing").classList.add' in line)

    assert fetch_at < hide_at, (
        "the note has to arrive before the listing goes away, or a failure "
        "leaves the operator with neither"
    )


def test_the_open_handler_catches_its_own_rejection() -> None:
    """`openNote` is async and this is its only caller."""

    page = _page()

    assert "openNote(row.note_id).catch(showError)" in page


def test_every_page_contributes_its_folders() -> None:
    """Folders came from the first page alone.

    `Load more` grew the note list without growing the folder list, so a folder
    whose notes sort after the first fifty was unreachable by navigation --
    present in the corpus, absent from the only control that walks into it.
    """

    page = _page()

    assert "renderFolders(page.notes)" in page
    assert "if (!append) crumbs();" in page, (
        "breadcrumbs describe the current prefix and are painted once; folders "
        "accumulate and are not"
    )


def test_a_folder_is_not_listed_twice() -> None:
    """Appending is per page, so the guard has to be per folder."""

    page = _page()

    assert "FOLDERS.includes(folder)" in page
    assert "FOLDERS = []" in page, "a fresh listing starts with no folders shown"


def test_the_filter_fields_carry_labels_rather_than_only_placeholders() -> None:
    """A placeholder is not an accessible name, and it vanishes when the field
    has content -- so the moment a reader most needs to know what a value is
    filtering is the moment the hint disappears."""

    page = _page()

    assert '<label class="field" for="filter-tag">' in page
    assert '<label class="field" for="filter-facet">' in page


def test_a_span_can_be_chosen_without_a_mouse() -> None:
    """A `pre` is not focusable and shift+arrow does not select inside one, so
    a drag was the only way in and the core action of this console was
    unreachable from the keyboard."""

    page = _page()

    assert "function spanFromLines(from, to)" in page
    assert 'fromInput.type = "number"' in page
    assert "const span = selectedSpan() || spanFromLines(1, 1);" in page


def test_both_ways_of_choosing_produce_the_same_kind_of_span() -> None:
    """Line numbers resolve to offsets, which is what a selection resolves to.

    Two code paths would be two definitions of what a span is, and only one of
    them would keep matching the stored text. `current` preserves the exact
    mouse span until a line-input handler explicitly replaces it.
    """

    page = _page()

    assert "const span = current;" in page
    assert "occurrence: occurrenceOf(NOTE.body, span.expected, span.start)" in page
    assert "expected_text: span.expected" in page


def test_a_mouse_selection_seeds_the_line_inputs() -> None:
    """So the two ways of choosing agree about what is chosen, and a reader can
    see the selection they made expressed as something they can adjust."""

    page = _page()

    assert "function lineOf(offset)" in page
    assert "const first = lineOf(current.start);" in page


def test_retargeting_the_lines_keeps_words_already_written() -> None:
    """Re-seeding is only safe while the replacement still matches the span it
    was seeded from; after that, changing lines re-aims the edit rather than
    discarding what the operator typed."""

    page = _page()

    assert "let pristine = true;" in page
    assert "if (pristine) {" in page


def test_cancelling_returns_focus_to_the_control_that_opened_it() -> None:
    """Otherwise focus lands on the document and a keyboard user tabs from the
    top of the page to get back."""

    page = _page()

    assert '$("propose-open").focus();' in page

