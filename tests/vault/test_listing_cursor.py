"""The listing cursor, as a value rather than through an endpoint.

`test_note_listing` covers what a caller sees: a walk that does not skip, and
a refusal for every cursor that cannot be honoured. What is worth pinning here
is the codec's own edges -- padding, alphabet, and the shapes of malformed
input that a round-trip test never produces.
"""

import base64
import json

import pytest

from app.vault.cursors import (
    PATH_SORT,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)


@pytest.mark.parametrize(
    "key",
    [
        "Agent/notes/a.md",
        # Lengths chosen so the base64 payload lands on each padding case.
        "a",
        "ab",
        "abc",
        # A key is a vault_path today and a title later; neither is ASCII-only,
        # and both may carry the characters a query string cares about.
        "Human/03 Projects/Émile & co (draft).md",
        'a "quoted" key, with a comma',
    ],
)
def test_a_cursor_round_trips_whatever_key_it_was_given(key: str) -> None:
    token = encode_cursor(PATH_SORT, key, "note-1")

    assert decode_cursor(token, sort=PATH_SORT) == (key, "note-1")


def test_a_cursor_needs_no_escaping_in_a_query_string() -> None:
    """It travels as a query parameter, so `+`, `/` and `=` would all be
    mangled or re-encoded somewhere on the way back."""

    token = encode_cursor(PATH_SORT, "Agent/notes/a+b/c=d.md", "note-1")

    assert set(token) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_a_cursor_from_another_order_is_refused() -> None:
    """A position is a position *in an order*; the same row sits somewhere
    different in each one. Re-seating the cursor would resume at an unrelated
    row and skip everything between."""

    token = encode_cursor("updated", "Agent/notes/a.md", "note-1")

    with pytest.raises(InvalidCursor) as refusal:
        decode_cursor(token, sort=PATH_SORT)

    assert "'updated'" in str(refusal.value)
    assert "start the listing again" in str(refusal.value)


def _token(payload: object) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    ("token", "why"),
    [
        ("", "empty"),
        ("!!!!", "not base64"),
        (base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="), "not utf-8"),
        (base64.urlsafe_b64encode(b"not json").decode().rstrip("="), "not json"),
        (_token(["path", "k", "i"]), "json, but not an object"),
        (_token("path"), "json, but a bare string"),
        (_token({"s": PATH_SORT, "k": "a"}), "no id"),
        (_token({"s": PATH_SORT, "i": "note-1"}), "no key"),
        (_token({"s": PATH_SORT, "k": 7, "i": "note-1"}), "key is not text"),
        (_token({"s": PATH_SORT, "k": "a", "i": None}), "id is not text"),
    ],
)
def test_a_malformed_cursor_is_refused(token: str, why: str) -> None:
    """Every shape refuses, and none of them raises something else.

    A `TypeError` escaping this module would reach the transport as a 500 --
    an unreadable cursor reported as the server's fault rather than the
    request's.
    """

    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort=PATH_SORT)
