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
    MAX_CURSOR_CHARS,
    PATH_SORT,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)


# `vault_documents_vault_path_format`. Restated rather than imported: it lives
# in a CHECK constraint as SQL text, and the point of the test below is that
# nothing connects the two automatically.
VAULT_PATH_MAX_CHARS = 1024


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


def _token(payload: object) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    ("label", "character"),
    [
        ("ascii", "a"),
        # Two bytes as UTF-8, six if escaped into ASCII first.
        ("accented", "é"),
        # Four bytes as UTF-8, and one character to the CHECK constraint.
        ("astral", "😀"),
        # The worst the encoder can produce: escaped whatever else is set.
        ("control", ""),
    ],
)
def test_the_bound_covers_the_longest_cursor_this_corpus_can_issue(
    label: str, character: str
) -> None:
    """`after`'s bound must never refuse what `/notes` just handed out.

    It did, until review caught it: the bound was 1024 because that is what a
    vault_path may measure, and a cursor is the path plus JSON plus base64 --
    half as long again for ASCII, and far worse before `ensure_ascii` was
    turned off. A corpus with paths near the limit would have been issued
    cursors this endpoint then rejected, and paging would have stopped dead at
    exactly the deepest paths.

    Two numbers in two files with nothing between them, so the arithmetic is
    asserted rather than trusted.
    """

    worst = encode_cursor(
        PATH_SORT, character * VAULT_PATH_MAX_CHARS, "f" * 32
    )

    assert len(worst) <= MAX_CURSOR_CHARS, (
        f"a {label} path at the {VAULT_PATH_MAX_CHARS}-character limit encodes "
        f"to {len(worst)} characters, past the {MAX_CURSOR_CHARS} `after` will "
        "accept"
    )


@pytest.mark.parametrize(
    ("damage", "corrupt"),
    [
        # The one that got through. `base64.urlsafe_b64decode` discards every
        # character outside the alphabet instead of objecting to it, so this
        # decoded to the payload underneath and was honoured.
        ("junk appended", lambda token: token + "!!!!"),
        ("one junk character", lambda token: token + "!"),
        ("trailing whitespace", lambda token: token + " "),
        ("whitespace inside", lambda token: token[:8] + " " + token[8:]),
        # `=` is stripped on the way out and put back on the way in, so a
        # token carrying its own padding is not the one that was issued --
        # spelled as a literal rather than computed, because a computed pad is
        # empty whenever the token already sits on a multiple of four.
        ("padding restored", lambda token: token + "="),
        ("truncated", lambda token: token[:-4]),
        ("a character swapped", lambda token: token[:-1] + ("A" if token[-1] != "A" else "B")),
    ],
)
def test_a_corrupted_cursor_is_refused_rather_than_repaired(
    damage: str, corrupt
) -> None:
    """Damage to a valid token must not decode to the token underneath it.

    This is the failure the "damaged cursors are refused" promise was making
    without keeping. A cursor that survives corruption is worse than one that
    fails: the caller is handed a page from a position they did not ask for,
    and nothing anywhere says so.

    Canonical or nothing -- a cursor is what this endpoint issued, byte for
    byte -- because the alternative is finding these one shape at a time.
    """

    token = encode_cursor(PATH_SORT, "Agent/notes/a.md", "note-1")

    with pytest.raises(InvalidCursor):
        decode_cursor(corrupt(token), sort=PATH_SORT)


def test_a_cursor_python_can_parse_but_not_re_encode_is_refused() -> None:
    """The canonicalisation must not be able to raise past its own caller.

    A lone surrogate is a legal JSON escape and an illegal UTF-8 character,
    so the payload below decodes, parses, and passes every type check -- and
    then fails when the canonical check re-encodes it. That failure escaped
    `decode_cursor`,
    and `_resume_after` catches only `InvalidCursor`, so a forged cursor was
    answered with a 500: the server reporting a malformed request as its own
    fault.
    """

    token = base64.urlsafe_b64encode(
        r'{"s":"path","k":"\ud800","i":"x"}'.encode("ascii")
    ).decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort=PATH_SORT)


def test_a_cursor_carrying_a_nul_is_refused_before_it_reaches_postgres() -> None:
    """Canonical is not the same as bindable.

    A forger can encode any key they like, and the canonical check only asks
    that the token be what this encoder would have produced for it -- so a NUL
    passes every shape test here and then meets psycopg, which refuses to bind
    one into a text parameter and raises `DataError` from inside the query.
    That surfaced as a 500 for a request the caller had made wrong.

    Refused here instead, where it is cheap and where the message can say what
    kind of thing went wrong. Nothing valid is turned away: the column could
    not have stored such a path in the first place.
    """

    for key, note_id in (
        ("Agent/notes/a\x00b.md", "note-1"),
        ("Agent/notes/a.md", "note\x001"),
    ):
        token = encode_cursor(PATH_SORT, key, note_id)

        with pytest.raises(InvalidCursor):
            decode_cursor(token, sort=PATH_SORT)


def test_a_cursor_whose_json_will_not_convert_is_refused() -> None:
    """Not every parse failure is a `JSONDecodeError`.

    Python refuses to convert an integer literal of more than 4300 digits and
    raises a plain `ValueError` doing it, so a payload that is perfectly well
    formed JSON can still fail to parse. The guard named `JSONDecodeError` and
    `UnicodeDecodeError`, so this one escaped `decode_cursor` and came back as
    a 500 -- the same shape as the surrogate, and reachable for the same
    reason: a caller chooses the payload.

    The size is the point. 5000 digits encode to well under the bound `after`
    accepts, so nothing else stops this first.
    """

    digits = 5000
    payload = '{"s":"path","k":' + "1" * digits + ',"i":"x"}'
    token = (
        base64.urlsafe_b64encode(payload.encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )

    assert len(token) < MAX_CURSOR_CHARS, "the bound would refuse this first"

    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort=PATH_SORT)


def test_a_cursor_nested_past_the_parser_is_refused() -> None:
    """Reachable on the interpreter that runs this, which is not this one.

    CPython 3.12 -- pinned by `.python-version` for CI and production -- raises
    `RecursionError` out of the JSON C scanner at 2997 nested arrays, a token
    of 8026 characters and so comfortably inside the bound `after` accepts.
    `RecursionError` is not a `ValueError`, so it escaped the parse guard and
    came back as a 500 on input the caller chooses.

    The assertion is the promise rather than the mechanism, because the
    mechanism differs by interpreter: on 3.12 the parser raises, and on 3.14
    the payload parses and is then refused by the type checks below. Both
    answer `InvalidCursor`, which is the only thing a caller sees. A local
    probe on 3.14 is what reported this unreachable in the first place.
    """

    depth = 3000
    payload = '{"s":"path","k":' + "[" * depth + "]" * depth + ',"i":"x"}'
    token = (
        base64.urlsafe_b64encode(payload.encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )

    assert len(token) < MAX_CURSOR_CHARS, "the bound would refuse this first"

    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort=PATH_SORT)


def test_a_cursor_with_no_sort_is_malformed_rather_than_foreign() -> None:
    """Two different failures that shared one message.

    A token carrying no sort is not a cursor from another order; it is not a
    cursor. Reported as the latter it read "cursor belongs to the None order",
    which names nothing a caller can do anything about.
    """

    for payload in ({"k": "a", "i": "b"}, {"s": 7, "k": "a", "i": "b"}):
        with pytest.raises(InvalidCursor) as refusal:
            decode_cursor(_token(payload), sort=PATH_SORT)

        assert "not a valid token" in str(refusal.value)
        assert "order" not in str(refusal.value)


def test_a_cursor_from_another_order_is_refused() -> None:
    """A position is a position *in an order*; the same row sits somewhere
    different in each one. Re-seating the cursor would resume at an unrelated
    row and skip everything between."""

    token = encode_cursor("updated", "Agent/notes/a.md", "note-1")

    with pytest.raises(InvalidCursor) as refusal:
        decode_cursor(token, sort=PATH_SORT)

    assert "'updated'" in str(refusal.value)
    assert "start the listing again" in str(refusal.value)


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
