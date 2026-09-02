"""The session module is shared by inclusion, and knows nothing about reviewing.

The OAuth lifecycle in this module cost six review rounds and nine findings to
stabilise: cross-tab rotation burning the family, a persisted token nothing
presented, a renewal that left an inert page, a startup body no test could
drive. A second console that copied it would inherit every one of those bugs
into a file where they would have to be found again.

These tests pin the two properties that make sharing real rather than nominal:
the page includes the module instead of restating it, and the module carries no
console-specific literal that a second console would have to edit out.
"""

import re

import pytest

from app.vault.review_console import (
    CLIENT_NAME,
    REVIEW_PATH,
    STORE_PREFIX,
)
from app.vault.templating import TEMPLATE_DIRECTORY
from tests.vault.test_review_console import _page, _session_module


def _review_template() -> str:
    return (TEMPLATE_DIRECTORY / "review.html").read_text(encoding="utf-8")


def test_the_review_console_includes_the_module_rather_than_restating_it() -> None:
    template = _review_template()

    assert '{% include "_console_session.js" %}' in template
    for owned_by_the_module in (
        "async function refreshTokens()",
        "async function withRefreshLock(",
        "async function resumeSession()",
        "async function boot()",
    ):
        assert owned_by_the_module not in template, (
            f"{owned_by_the_module} is written in the page as well as the "
            "shared module; one of the two copies will drift"
        )
        assert owned_by_the_module in _page()


def test_the_module_is_included_into_the_page_script_not_beside_it() -> None:
    """One script, so the module's `let` bindings and the page's functions
    share a scope -- which is what they did before the extraction.

    Two `<script>` blocks would mostly work and fail in one specific way: a
    `const` in the first is not hoisted for the second, so an ordering mistake
    would surface as a ReferenceError inside a broad catch, which is exactly
    the class of bug this file already has a regression test for.
    """

    template = _review_template()
    include_at = template.index('{% include "_console_session.js" %}')
    # Matched rather than searched for literally: the tag carries a CSP nonce
    # attribute, so `<script>` no longer appears verbatim.
    openers = list(re.finditer(r"(?m)^<script\b[^>]*>", template[:include_at]))
    assert openers, "no script tag opens before the include"
    between = template[openers[-1].end() : include_at]

    assert between.strip() == '"use strict";'
    assert "</script>" not in between, (
        "the include lands outside the script that runs it"
    )


@pytest.mark.parametrize(
    "literal",
    [
        STORE_PREFIX,
        CLIENT_NAME,
        REVIEW_PATH,
        "reviewPath",
    ],
)
def test_the_module_names_nothing_about_the_reviewer(literal: str) -> None:
    """Everything console-specific arrives through CFG.

    This is the test that makes the second console cheap. A literal left here
    is not a style problem: a shared `storePrefix` would have two pages writing
    one session record and presenting each other's refresh tokens, which the
    authorization server reads as a captured credential and answers by burning
    the family.

    Comments are deliberately in scope. `vault:review` is named in a couple of
    them as the motivating case for keeping a family alive, which is fair; what
    must not appear is a value the module *uses*.
    """

    assert literal not in _session_module()


def test_the_module_takes_its_storage_and_lock_from_the_prefix() -> None:
    """Both, because they are the two ways two consoles can collide."""

    module = _session_module()

    assert 'CFG.storePrefix + ".session"' in module
    assert 'CFG.storePrefix + ".token"' in module
    assert 'navigator.locks.request(CFG.storePrefix + ".refresh", work)' in module


def test_sign_in_is_guarded_at_the_function_boundary() -> None:
    """Every present and future caller shares one registration/PKCE attempt."""

    module = _session_module()

    assert "let SIGN_IN_ATTEMPT = null;" in module
    assert "if (SIGN_IN_ATTEMPT) return SIGN_IN_ATTEMPT;" in module


def test_the_legacy_keys_are_derived_from_the_same_prefix() -> None:
    """The upgrade path has to keep matching what the reviewer console wrote.

    `vault.review.client_id` and `vault.review.refresh` are the keys of the
    session-scoped format this replaced. They are reached as prefix + suffix,
    so `STORE_PREFIX` is what makes the migration still find them -- and a
    console that never used that format finds nothing, which is correct.
    """

    module = _session_module()

    assert 'CFG.storePrefix + ".client_id"' in module
    assert 'CFG.storePrefix + ".refresh"' in module
    assert STORE_PREFIX == "vault.review", (
        "the legacy sessionStorage keys are derived from this prefix; changing "
        "it silently ends every live reviewer session and costs a re-grant"
    )


def test_the_page_supplies_what_the_module_expects_of_it() -> None:
    """The module calls `render()` and paints into `#messages` and `#who`."""

    page = _page()

    assert "function render()" in page
    assert 'id="messages"' in page
    assert 'id="who"' in page


def test_the_console_config_carries_every_key_the_module_reads() -> None:
    """A missing key renders as `undefined` in a storage key or a redirect URI,
    which fails at sign-in rather than at render."""

    page = _page()

    for key in ("apiBase", "scopes", "consolePath", "clientName", "storePrefix"):
        assert f'"{key}"' in page
        assert f"CFG.{key}" in page
