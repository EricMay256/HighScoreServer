"""The listing cursor: a position in an ordered walk, and which walk it was.

`/notes` pages by keyset rather than by OFFSET, so a cursor names the last row
of the previous page and the next page starts after it. Until ADR 0045 that
cursor *was* a `vault_path` -- the endpoint said so in its own OpenAPI
description -- which worked because `vault_path` is UNIQUE and therefore a
total order all by itself.

Sorting broke both halves of that. `updated_at`, `created_at` and `title` are
none of them unique, so the key alone cannot separate two rows that share it,
and a cursor carrying only the key would skip or repeat every row in a tie. And
a cursor that is legible is a cursor callers read, which would make the paging
key part of the contract exactly when it needs to vary.

So a cursor is opaque and carries three things: the sort it belongs to, the key
it stopped at, and the id that breaks the tie.

**Not signed, and not a capability.** A cursor names a position in a listing the
credential could already read, and the read policy is applied inside the query
on every page rather than to the cursor -- so the worst a forged one can do is
start the caller's own listing somewhere else in their own listing. An HMAC
here would buy nothing and would imply the token authorizes something, which is
the misreading worth preventing.

Opaque means unreadable, not unforgeable. It is base64url, and anyone may
decode it; what it is not is *promised*.
"""

import base64
import binascii
import json
from typing import Final


__all__ = [
    "MAX_CURSOR_CHARS",
    "PATH_SORT",
    "InvalidCursor",
    "decode_cursor",
    "encode_cursor",
]


# The default walk, and in phase 1 the only one. Named rather than spelled
# inline so the token's contents and the request's parameter cannot disagree
# by a typo; ADR 0045's later phases replace it with a `NoteSort` enum.
PATH_SORT: Final = "path"

# The bound `after` is validated against. It exists so a caller pasting a book
# into the parameter is refused before anything decodes it -- not to be tight.
#
# It has to clear the longest cursor this endpoint can *issue*, which is the
# bug review found here: the bound was 1024, sized for the vault_path a cursor
# used to be, while the token wrapping that path is half as long again. A
# corpus with paths near the limit would have been handed cursors it then
# refused.
#
# `vault_documents_vault_path_format` caps a path at 1024 characters. A
# character costs at most six bytes once JSON escaping and UTF-8 are both
# allowed for, the rest of the payload is under a hundred, and base64 adds a
# third -- which lands under 10240 with room to spare. A real vault path is
# tens of characters and its cursor is around a hundred.
#
# The id is not bounded by the schema (`vault_documents_id_nonempty` checks
# only that it is not blank) and is minted as 32 hex characters. A deliberately
# pathological id could therefore still exceed this, and the failure direction
# is then a refusal rather than a wrong page.
#
# `test_listing_cursor` pins this against the path limit, because that 1024
# lives in a CHECK constraint with no import between it and here.
MAX_CURSOR_CHARS: Final = 10240


class InvalidCursor(Exception):
    """A cursor that cannot be honoured, with the reason a caller can act on.

    Every way of being wrong arrives here as one exception on purpose. The
    transport answers all of them with 422, and distinguishing "not base64"
    from "wrong sort" in the status code would offer a caller a choice they do
    not have: in every case the fix is to start the walk again.
    """


def encode_cursor(sort: str, key: str, note_id: str) -> str:
    """The token naming the row a page ended on.

    Padding is stripped because `=` is not URL-safe in every context this
    travels through, and `decode_cursor` puts it back -- the length is
    recoverable, so carrying it is only a way to be mangled in transit.
    """

    payload = json.dumps(
        {"s": sort, "k": key, "i": note_id},
        separators=(",", ":"),
        # The payload is base64'd as UTF-8, so escaping non-ASCII into ASCII
        # escapes first would spend six bytes a character to say what two or
        # three already said -- and a vault path may hold any of them. Left on
        # by default, an accented path inflated its cursor sixfold.
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str, *, sort: str) -> tuple[str, str]:
    """The `(key, id)` a page should resume after, for this sort.

    Refuses a token belonging to a different sort rather than re-seating it
    into this one. A cursor is a position in *an order*, and the same row sits
    at a different position in every order -- so honouring a recency cursor
    against a path walk would resume somewhere unrelated and silently skip
    everything between. Changing sort is a new walk, and a new walk starts at
    the beginning.
    """

    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as error:
        raise InvalidCursor("cursor is not a valid token") from error

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCursor("cursor is not a valid token") from error

    if not isinstance(payload, dict):
        raise InvalidCursor("cursor is not a valid token")

    carried, key, note_id = payload.get("s"), payload.get("k"), payload.get("i")
    # The sort is checked for shape here and for agreement below, and those are
    # different failures. A token carrying no sort at all is malformed, not a
    # cursor belonging to some other order -- reporting it as the latter told
    # the caller their cursor "belongs to the None order", which names nothing
    # they can act on.
    if (
        not isinstance(carried, str)
        or not isinstance(key, str)
        or not isinstance(note_id, str)
    ):
        raise InvalidCursor("cursor is not a valid token")
    if carried != sort:
        raise InvalidCursor(
            f"cursor belongs to the {carried!r} order, not {sort!r}; "
            "start the listing again after changing sort"
        )
    return key, note_id
