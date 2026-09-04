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


__all__ = ["InvalidCursor", "PATH_SORT", "decode_cursor", "encode_cursor"]


# The default walk, and in phase 1 the only one. Named rather than spelled
# inline so the token's contents and the request's parameter cannot disagree
# by a typo; ADR 0045's later phases replace it with a `NoteSort` enum.
PATH_SORT: Final = "path"


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
        {"s": sort, "k": key, "i": note_id}, separators=(",", ":")
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
    if not isinstance(key, str) or not isinstance(note_id, str):
        raise InvalidCursor("cursor is not a valid token")
    if carried != sort:
        raise InvalidCursor(
            f"cursor belongs to the {carried!r} order, not {sort!r}; "
            "start the listing again after changing sort"
        )
    return key, note_id
