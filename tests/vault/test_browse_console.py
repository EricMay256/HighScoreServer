"""The browse console: a second page holding a second credential.

What is worth pinning here is mostly what makes it *separate*. Two consoles in
one browser that share a storage namespace share a session record and present
each other's refresh tokens, which the authorization server reads as a captured
credential and answers by burning the family. The reviewer's scope set and this
one are also kept apart on purpose: `vault:review` may be granted only to a
family holding `vault:read` alone, so a console that could do both would be a
console that could do neither properly.
"""

import re

import pytest
from starlette.responses import HTMLResponse
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


def _console_response() -> HTMLResponse:
    return console_page(
        "browse.html",
        console_path=browse.BROWSE_PATH,
        scopes=browse.CONSOLE_SCOPES,
        client_name=browse.CLIENT_NAME,
        store_prefix=browse.STORE_PREFIX,
    )


@pytest.mark.parametrize(
    "header",
    [
        "X-Frame-Options",
        "Referrer-Policy",
        "Cache-Control",
    ],
)
def test_the_console_sets_its_protective_headers(header: str) -> None:
    """Served from the shared helper, so both consoles carry the same set.

    A second copy of this policy is a policy that drifts, and it drifts
    silently: the weaker page keeps working.

    The CSP is not in this list because it carries a per-response nonce and so
    is not a constant; the two tests below cover it.
    """

    assert _console_response().headers[header] == CONSOLE_HEADERS[header]


def test_inline_script_is_allowed_by_nonce_and_not_by_unsafe_inline() -> None:
    """'unsafe-inline' permits an injected inline script; a nonce does not.

    The pages inline their script, so something has to permit it. Whichever it
    is applies to *every* inline script the document ends up containing, which
    is precisely the case the directive exists to govern -- so it is the nonce,
    which names only the blocks this render emitted.

    Defence in depth. The consoles build their DOM with `textContent` and never
    interpolate corpus text into markup, so there is no known injection this
    closes.
    """

    response = _console_response()
    csp = response.headers["Content-Security-Policy"]
    script_src = next(
        part.strip() for part in csp.split(";") if part.strip().startswith("script-src")
    )

    assert "'unsafe-inline'" not in script_src
    assert "'nonce-" in script_src

    # Every inline script in the page must carry that exact nonce, or the page
    # is served broken rather than served insecure.
    #
    # Line-anchored: every real script tag in these templates starts a line,
    # and `_console_session.js` mentions "<script>" in a comment that is inside
    # a script body rather than opening one.
    nonce = script_src.split("'nonce-", 1)[1].split("'", 1)[0]
    tags = re.findall(r"(?m)^<script\b[^>]*>", response.body.decode())

    assert len(tags) >= 2, "expected the config block and the page script"
    for tag in tags:
        assert f'nonce="{nonce}"' in tag, f"inline script without the nonce: {tag}"


def test_the_script_nonce_stays_inside_the_unambiguous_alphabet() -> None:
    """Hex, which is a strict subset of what CSP's grammar accepts.

    CSP3's `base64-value` is
    `1*( ALPHA / DIGIT / "+" / "/" / "-" / "_" )*2( "=" )`, so the base64url
    characters a `token_urlsafe` nonce can contain were never out of spec.
    Hex simply cannot raise the question, and the nonce is machine-generated
    with no other constraint on its shape, so there is nothing to trade away.

    Pinned because the reason is not visible from the call: `token_urlsafe`
    reads like the obvious choice for a header value.
    """

    csp = _console_response().headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]

    assert re.fullmatch(r"[0-9a-f]+", nonce), nonce
    # 16 bytes of entropy, per _NONCE_BYTES.
    assert len(nonce) == 32


def test_the_script_nonce_is_fresh_on_every_response() -> None:
    """A reused nonce is 'unsafe-inline' with extra steps.

    Safe to vary per response only because these pages are `no-store`: a
    cached body would carry a nonce its header no longer names and would
    silently stop running.
    """

    first = _console_response()
    second = _console_response()

    assert first.headers["Content-Security-Policy"] != (
        second.headers["Content-Security-Policy"]
    )
    assert CONSOLE_HEADERS["Cache-Control"] == "no-store"


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
    end = next(
        i for i, line in enumerate(lines[start + 1 :], start + 1)
        if line.startswith("/* ---------- Proposing an edit")
    )
    body = lines[start:end]

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
    assert "Number.isInteger(from)" in page
    assert 'fromInput.step = "1"' in page
    assert 'toInput.step = "1"' in page
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


def test_edges_are_links_that_name_notes_by_slug() -> None:
    """A hex uuid is not a name, and ADR 0025 says a human never sees one.

    Edges are stored as ids and stay that way, so the console resolving them
    is the boundary that ADR describes -- the same one the export crosses when
    it writes `[[slug]]`. The label is the slug rather than the title because
    that is what a wikilink carries, so a link here and a link in the exported
    tree name a note identically.
    """

    page = _page()

    assert "/notes/edges?" in page, "edges must be resolved in bulk"
    assert "edge.slug" in page
    assert "openNote(edge.note_id)" in page, "a resolved edge must be clickable"


def test_edge_resolution_is_one_request_for_the_whole_note() -> None:
    """Not one per edge, which is the bug this pattern already caused once.

    The review console fetched every evidence note while painting its queue
    and exhausted the `get_note` burst doing it. A note with five edges is the
    same shape, so the ids are gathered and resolved together -- deduplicated
    across `related_ids` and `source_ids`, which commonly overlap.
    """

    page = _page()

    assert "new Set([...detail.related_ids, ...detail.source_ids])" in page
    # Resolution happens before the panel is painted, so the note does not
    # render and then rearrange itself.
    assert page.index("resolveEdges(") < page.index('const panel = $("note");')


def test_an_unresolvable_edge_is_shown_but_not_linked() -> None:
    """It is an edge that points somewhere the reader cannot go.

    Three situations arrive as one -- no such note, withheld by the read
    policy, flagged -- because the endpoint declines to distinguish them, and
    saying which would confirm the id exists. Showing the bare id is honest;
    hiding it would make the note look less connected than it is.
    """

    page = _page()

    assert 'el("span", "mono muted", id)' in page
    assert "An unresolved id names no note you can open" in page


def test_an_unresolvable_edge_says_so_in_text_not_a_tooltip() -> None:
    """Colour and a `title` are not available to every reader.

    The marker was a `title` on a non-focusable `<span>`, with muted colour as
    the only visible cue. A keyboard user cannot reach that tooltip, a screen
    reader treats it inconsistently, and colour alone is not a distinction --
    so the one signal that an id is *not* a link reached only a sighted mouse
    user who happened to hover it.

    It is now text: a per-id marker saying which, and one explanation saying
    what it means.
    """

    page = _page()

    assert '" (unresolved)"' in page
    # The dangling span must carry no tooltip standing in for that text.
    assert "dangling.title" not in page


def test_a_failed_edge_lookup_is_not_reported_as_corpus_state() -> None:
    """A 429 is not evidence that a note was retired.

    resolveEdges swallowed every non-authentication failure into an empty map,
    so a rate-limited, unavailable or dropped request rendered identically to
    ids the corpus genuinely cannot resolve -- and the list then told the
    reader those notes may have been retired or may be unreadable. That is an
    infrastructure failure restated as fact about the corpus, and it is easy
    to reach: the resolve_edges bucket bursts at 20.

    The lookup now reports its own failure, the ids are shown as stored, and
    nothing claims to know what they point at.
    """

    page = _page()

    assert "failed: true" in page, "a failed lookup must be distinguishable"
    assert '" (not looked up)"' in page
    assert "could not be looked up just now" in page
    assert "This says nothing about whether the notes exist" in page
