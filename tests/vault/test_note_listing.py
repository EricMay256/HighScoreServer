"""Browsing the corpus by where notes live.

`/search` ranks and `/notes/{id}` fetches; this is the third thing, and the one
a human needs first -- looking around. The assertions worth having are about
what a listing must *not* do: serve a note the read policy withholds, return a
short page because it filtered after the query, or hand back a cursor that
skips whatever it dropped.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.vault.api_models import (
    MAX_EDGE_LOOKUP_IDS,
    VaultNoteListResponse,
    VaultNoteSummary,
)
from app.vault.auth import VaultScope
from app.vault.cursors import decode_cursor, encode_cursor
from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    NoteSort,
    VaultDocumentBrief,
)
from app.vault.repository import (
    DOCUMENT_BRIEF_COLUMNS,
    DOCUMENT_DOMAIN_COLUMNS,
    SORT_KEYS,
    VaultDocumentRepository,
    path_page_statement,
)
from app.vault.settings import vault_enabled
from app.vault.tables import vault_documents
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PREFIX = "test-listing-"

# Six notes chosen so that every rule the endpoint applies has exactly one row
# that proves it: a status filter, a path policy, two facet axes, and a tag
# that two notes share.
CORPUS = (
    # suffix, path, status, tags, facets
    (
        "alpha",
        "Human/03 Projects/alpha.md",
        DocumentStatus.ACTIVE,
        ("hss", "api"),
        {"project": ["hss"]},
    ),
    (
        "beta",
        "Human/03 Projects/beta.md",
        DocumentStatus.ACTIVE,
        ("hss",),
        {"project": ["b2"], "area": ["ops"]},
    ),
    (
        "gamma",
        "Human/04 Areas/gamma.md",
        DocumentStatus.ACTIVE,
        (),
        {},
    ),
    # Archived is readable: retired history a reference may still point at.
    (
        "delta",
        "Agent/notes/delta.md",
        DocumentStatus.ARCHIVED,
        (),
        {},
    ),
    # Flagged is withheld. The write path declined to endorse it, and a
    # listing is exactly the surface that would present it as ordinary.
    (
        "epsilon",
        "Agent/notes/epsilon.md",
        DocumentStatus.FLAGGED,
        (),
        {},
    ),
    # Outside READABLE_PATH_PREFIXES: governance says no, whatever its status.
    (
        "zeta",
        "Human/02 Private/zeta.md",
        DocumentStatus.ACTIVE,
        (),
        {},
    ),
)


@pytest.fixture
def corpus(configure_test_env: None) -> dict[str, str]:
    """The six notes above, removed afterwards whatever the test asserted."""

    run = uuid4().hex[:8]
    ids = {suffix: f"{PREFIX}{run}-{suffix}" for suffix, *_ in CORPUS}
    transactions, engine = vault_service()

    async def seed() -> None:
        async with transactions.transaction() as connection:
            documents = VaultDocumentRepository()
            for suffix, path, status, tags, facets in CORPUS:
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=ids[suffix],
                        kind=DocumentKind.NOTE,
                        doc_type="Agent Note",
                        # The run id keeps two runs from colliding on
                        # vault_path, which is UNIQUE.
                        vault_path=path.replace(".md", f"-{run}.md"),
                        status=status,
                        doc_status="Active",
                        title=f"Listing fixture {suffix}",
                        body=f"Body of {suffix}.",
                        summary=f"Summary of {suffix}." if suffix != "gamma" else None,
                        tags=tags,
                        facets=facets,
                        contributed_by=f"agent:{PREFIX}seed",
                        provenance={"fixture": True},
                    ),
                )

    async def clear() -> None:
        async with transactions.transaction() as connection:
            await connection.execute(
                delete(vault_documents).where(
                    vault_documents.c.id.like(f"{PREFIX}{run}-%")
                )
            )

    try:
        asyncio.run(seed())
        yield ids
    finally:
        asyncio.run(clear())
        asyncio.run(engine.dispose())


@pytest.fixture
def read_only_token(configure_test_env: None) -> str:
    credential_id, token = _issue(scopes=(VaultScope.READ,))
    try:
        yield token
    finally:
        _drop(credential_id)


# The codec speaks in sort names, and `NoteSort` is where they are defined.
PATH_SORT = NoteSort.PATH.value


def _list(client: TestClient, token: str, **params) -> dict:
    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _paths(payload: dict, ids: dict[str, str]) -> list[str]:
    """The fixture suffixes present in a response, in the order returned."""

    by_id = {note_id: suffix for suffix, note_id in ids.items()}
    return [
        by_id[note["note_id"]]
        for note in payload["notes"]
        if note["note_id"] in by_id
    ]


def test_listing_requires_a_credential(client: TestClient) -> None:
    response = client.get("/api/v1/vault/notes")

    assert response.status_code == 401


def test_listing_requires_the_read_scope(client: TestClient) -> None:
    credential_id, token = _issue(scopes=(VaultScope.PROPOSE,))
    try:
        response = client.get(
            "/api/v1/vault/notes",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 403


def test_a_listing_withholds_what_the_read_policy_withholds(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """The two exclusions that matter, in one assertion each.

    `flagged` is content the write path declined to endorse, and an unreadable
    path is a governance decision made in folders.yml. Neither may appear in a
    listing, which is the surface most likely to present them as ordinary rows.
    """

    payload = _list(client, read_only_token, limit=100)
    present = _paths(payload, corpus)

    assert "epsilon" not in present, "a flagged note must not be listed"
    assert "zeta" not in present, "an unreadable path must not be listed"
    assert "delta" in present, "archived is readable history, not exclusion"


def test_a_listing_is_ordered_by_vault_path(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """The corpus's own order. A listing sorted by relevance to no query would
    be arbitrary, and one sorted by time would scatter a folder across pages."""

    payload = _list(client, read_only_token, limit=100)
    paths = [note["vault_path"] for note in payload["notes"]]

    assert paths == sorted(paths)


def test_a_path_prefix_narrows_the_listing(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    payload = _list(client, read_only_token, path="Human/03 Projects/", limit=100)

    assert set(_paths(payload, corpus)) == {"alpha", "beta"}


def test_an_unreadable_prefix_lists_nothing_rather_than_refusing(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """What is readable is governance, not a permission this endpoint decides.

    An empty page is the honest answer: the caller asked about a place the read
    policy does not describe, and there is nothing there to report on.
    """

    payload = _list(client, read_only_token, path="Human/02 Private/", limit=100)

    assert payload["notes"] == []
    assert payload["has_more"] is False


def test_a_tag_filter_requires_every_tag(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Conjunctive. Two tags is a narrower question, never a wider one."""

    both = _list(client, read_only_token, tag=["hss", "api"], limit=100)
    one = _list(client, read_only_token, tag=["hss"], limit=100)

    assert set(_paths(both, corpus)) == {"alpha"}
    assert set(_paths(one, corpus)) == {"alpha", "beta"}


def test_a_facet_filter_selects_by_classification(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    payload = _list(client, read_only_token, facet=["project:hss"], limit=100)

    assert set(_paths(payload, corpus)) == {"alpha"}


def test_facets_on_different_axes_compose(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    matched = _list(
        client, read_only_token, facet=["project:b2", "area:ops"], limit=100
    )
    unmatched = _list(
        client, read_only_token, facet=["project:b2", "area:nowhere"], limit=100
    )

    assert set(_paths(matched, corpus)) == {"beta"}
    assert _paths(unmatched, corpus) == []


def test_incidental_whitespace_around_a_facet_is_dropped(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """A parser that strips when validating must strip when filtering.

    It did not, and disagreed with itself in both directions: `facet=
    project:hss` was refused as an unknown facet, and `facet=project: hss`
    filtered on a value with a leading space -- a filter that matches nothing
    and reports an empty page rather than an error, which is the worse half.
    """

    padded = _list(client, read_only_token, facet=["  project :  hss  "], limit=100)
    plain = _list(client, read_only_token, facet=["project:hss"], limit=100)

    assert _paths(padded, corpus) == _paths(plain, corpus) == ["alpha"]


def test_a_facet_value_keeps_the_spaces_inside_it(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Only the edges are incidental. A facet value may name something with a
    space in it, and collapsing that would filter on a value nobody stored."""

    payload = _list(
        client, read_only_token, facet=["project:not a real project"], limit=100
    )

    assert payload["notes"] == []


def test_an_unknown_facet_name_is_refused_rather_than_ignored(
    client: TestClient,
    read_only_token: str,
) -> None:
    """`FACET_NAMES` is closed, so `projects` is a typo -- and answering a typo
    with an unfiltered page answers a question nobody asked."""

    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {read_only_token}"},
        params={"facet": ["projects:hss"]},
    )

    assert response.status_code == 422
    assert "Unknown facet" in response.json()["detail"]


def test_a_malformed_facet_is_refused(
    client: TestClient,
    read_only_token: str,
) -> None:
    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {read_only_token}"},
        params={"facet": ["project"]},
    )

    assert response.status_code == 422
    assert "name:value" in response.json()["detail"]


def test_paging_walks_the_listing_without_repeating_or_skipping(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Keyset, so the cursor names the last row rather than an offset."""

    first = _list(client, read_only_token, path="Human/03 Projects/", limit=1)

    assert len(first["notes"]) == 1
    assert first["has_more"] is True
    # The cursor names that row -- it is no longer spelled as it (ADR 0045).
    assert decode_cursor(first["next_cursor"], sort=PATH_SORT) == (
        first["notes"][0]["vault_path"],
        first["notes"][0]["note_id"],
    )

    second = _list(
        client,
        read_only_token,
        path="Human/03 Projects/",
        limit=1,
        after=first["next_cursor"],
    )

    assert len(second["notes"]) == 1
    assert second["notes"][0]["note_id"] != first["notes"][0]["note_id"]
    assert _paths(first, corpus) + _paths(second, corpus) == ["alpha", "beta"]


def test_a_cursor_does_not_publish_the_row_it_names(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Opaque is the point: the paging key stops being part of the contract.

    While the cursor *was* a vault_path, every caller that looked at one
    learned the order and could construct the next. Sorting has to vary that
    key -- by `updated_at`, `created_at`, `title` -- and a key callers read is
    a key that cannot vary. So the token carries the position without
    spelling it.
    """

    first = _list(client, read_only_token, path="Human/03 Projects/", limit=1)
    cursor = first["next_cursor"]

    assert cursor != first["notes"][0]["vault_path"]
    assert cursor != first["notes"][0]["note_id"]
    # It travels in a query string, so it must need no escaping there.
    assert set(cursor) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_a_bare_vault_path_is_no_longer_a_cursor(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """The breaking half of ADR 0045, asserted rather than assumed.

    A caller holding yesterday's cursor gets a refusal naming the problem
    rather than a page starting somewhere arbitrary. Cursors are page-to-page
    ephemera, so there is no migration window to offer -- but there is a
    difference between refusing and quietly answering something else.
    """

    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {read_only_token}"},
        params={"path": "Human/03 Projects/", "after": "Human/03 Projects/alpha.md"},
    )

    assert response.status_code == 422, response.text
    assert "cursor" in response.json()["detail"]


def test_a_damaged_cursor_is_refused_rather_than_ignored(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Truncation in transit must not silently restart the walk.

    Ignoring an unreadable cursor would answer with page one, which a caller
    walking pages cannot distinguish from a corpus that changed under them --
    they would re-read what they had and never reach the end.
    """

    good = _list(client, read_only_token, path="Human/03 Projects/", limit=1)

    for damaged in (
        good["next_cursor"][:-4] + "zzzz",
        # Appended junk, which the base64 decoder used to discard silently --
        # so this exact request answered 200 with the page the intact cursor
        # named. See `test_listing_cursor` for the whole family.
        good["next_cursor"] + "!!!!",
    ):
        response = client.get(
            "/api/v1/vault/notes",
            headers={"Authorization": f"Bearer {read_only_token}"},
            params={"path": "Human/03 Projects/", "after": damaged},
        )

        assert response.status_code == 422, response.text


def test_a_cursor_from_another_order_is_refused(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """The reason the token carries its sort at all.

    No other sort exists yet, so this is the guard arriving before the thing
    it guards: once `sort=updated` lands, resuming a path walk from a recency
    cursor would start at an unrelated row and skip everything between it and
    where the caller actually was, silently.
    """

    foreign = encode_cursor("updated", "Human/03 Projects/alpha.md", "whatever")

    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {read_only_token}"},
        params={"path": "Human/03 Projects/", "after": foreign},
    )

    assert response.status_code == 422, response.text
    assert "start the listing again" in response.json()["detail"]


def _described_fields() -> dict[str, str]:
    """Every documented field of the listing's published schema.

    Nested models are read out of `$defs` rather than followed by hand: the
    row model is where the stale instruction actually lived, and it reaches a
    consumer through the response model's `$ref` like any other.
    """

    fields: dict[str, str] = {}
    for model in (VaultNoteListResponse, VaultNoteSummary):
        schema = model.model_json_schema()
        definitions = [(model.__name__, schema), *schema.get("$defs", {}).items()]
        for owner, definition in definitions:
            for name, field in definition.get("properties", {}).items():
                if "description" in field:
                    fields[f"{owner}.{name}"] = field["description"]
    return fields


def test_only_the_cursor_tells_a_caller_what_to_pass_as_after() -> None:
    """The paging contract is documented in one place or it is documented wrong.

    `after` stopped accepting a vault_path (ADR 0045), and the instruction to
    send one back was written in three places: the parameter, `next_cursor`,
    and `vault_path` on the row itself. Two rounds of review found them one at
    a time, which is what a manual sweep is worth against a description that
    can be copied anywhere.

    So: any field that tells a caller what to pass as `after` must be the
    cursor. A schema is what a client reads before it reads any prose here.
    """

    described = _described_fields()

    assert described, "the schema published no descriptions at all"

    instructing = {
        name: description
        for name, description in described.items()
        if "`after`" in description
    }

    assert set(instructing) == {"VaultNoteListResponse.next_cursor"}, (
        "these fields tell a caller what to pass as `after`, and only the "
        f"cursor may: {sorted(set(instructing) - {'VaultNoteListResponse.next_cursor'})}"
    )


def test_the_last_page_says_so(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """`has_more` is read from one row past the limit, so a full final page is
    still reported as final -- the case a "page looked full" guess gets wrong."""

    payload = _list(client, read_only_token, path="Human/03 Projects/", limit=2)

    assert len(payload["notes"]) == 2
    assert payload["has_more"] is False
    assert payload["next_cursor"] is None


def _stamp(ids: dict[str, str], column: str, moments: dict[str, datetime]) -> None:
    """Give named fixture notes distinct timestamps.

    Written directly rather than through the repository because the fixture
    seeds every note in one transaction, so `now()` gives all six the same
    `created_at` and `updated_at` to the microsecond. That tie is exactly what
    one test below needs and what the others cannot use.
    """

    transactions, engine = vault_service()

    async def apply() -> None:
        async with transactions.transaction() as connection:
            for suffix, moment in moments.items():
                await connection.execute(
                    update(vault_documents)
                    .where(vault_documents.c.id == ids[suffix])
                    .values(**{column: moment})
                )

    try:
        asyncio.run(apply())
    finally:
        asyncio.run(engine.dispose())


def test_a_time_order_reads_newest_first(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """What a path listing structurally cannot answer.

    Newest first, because that is the only direction anyone asks a recency
    question in -- which is why `NoteSort` carries no descending variants to
    double the cursor states for the other one.
    """

    base = datetime(2026, 9, 1, tzinfo=UTC)
    _stamp(corpus, "updated_at", {"alpha": base, "beta": base + timedelta(days=2)})

    payload = _list(
        client, read_only_token, path="Human/03 Projects/", sort="updated", limit=100
    )

    assert _paths(payload, corpus) == ["beta", "alpha"]


def test_the_two_time_orders_read_their_own_column(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """`created` and `updated` answer different questions, so set them against
    each other: the note written first is the one edited last.

    Two tables decide this between them -- `SORT_KEYS` names the column and
    `_CURSOR_KEYS` reads the key off the row -- with no import between them. A
    sort reading the wrong column would order plausibly and page wrongly, so
    it is asserted rather than assumed.
    """

    base = datetime(2026, 9, 1, tzinfo=UTC)
    _stamp(corpus, "created_at", {"alpha": base, "beta": base + timedelta(days=2)})
    _stamp(corpus, "updated_at", {"alpha": base + timedelta(days=4), "beta": base})

    by_created = _list(
        client, read_only_token, path="Human/03 Projects/", sort="created", limit=100
    )
    by_updated = _list(
        client, read_only_token, path="Human/03 Projects/", sort="updated", limit=100
    )

    assert _paths(by_created, corpus) == ["beta", "alpha"]
    assert _paths(by_updated, corpus) == ["alpha", "beta"]


def test_a_time_order_pages_across_rows_that_share_a_timestamp(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """The case a bare cursor gets wrong, and the reason for the tiebreaker.

    `vault_path` is UNIQUE, so a path cursor is a total order by itself. A
    timestamp is not: notes written in one transaction share it exactly, and a
    page boundary landing inside such a group would resume after every row
    holding that timestamp -- skipping the rest of the group -- or before it,
    repeating what it already returned. The id is what makes the order total.
    """

    rows = _list(
        client, read_only_token, path="Human/03 Projects/", sort="created", limit=100
    )["notes"]
    assert len({row["created_at"] for row in rows}) == 1, (
        "the fixture must seed these in one transaction for this to be a tie"
    )

    seen: list[str] = []
    cursor = None
    for _ in range(5):  # bounded, so a walk that loops fails instead of hanging
        page = _list(
            client,
            read_only_token,
            path="Human/03 Projects/",
            sort="created",
            limit=1,
            **({"after": cursor} if cursor else {}),
        )
        seen.extend(_paths(page, corpus))
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "the walk did not reach the end"
    assert sorted(seen) == ["alpha", "beta"], (
        f"a tied timestamp must be walked exactly once each; saw {seen}"
    )


def test_a_cursor_may_not_cross_between_orders(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Both directions, now that there is a second order to cross into.

    The same row sits somewhere different in every order, so resuming from
    another order's cursor starts at an unrelated position and skips whatever
    lies between -- silently, which is what makes it worth refusing rather
    than making a best effort of.
    """

    by_path = _list(client, read_only_token, path="Human/03 Projects/", limit=1)
    by_updated = _list(
        client, read_only_token, path="Human/03 Projects/", sort="updated", limit=1
    )

    crossings = (
        (by_path["next_cursor"], "updated"),
        (by_updated["next_cursor"], "path"),
        (by_updated["next_cursor"], "created"),
    )
    for cursor, sort in crossings:
        response = client.get(
            "/api/v1/vault/notes",
            headers={"Authorization": f"Bearer {read_only_token}"},
            params={"path": "Human/03 Projects/", "sort": sort, "after": cursor},
        )

        assert response.status_code == 422, response.text
        assert "start the listing again" in response.json()["detail"]


def test_the_default_order_is_still_the_path_order(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Additive, which is what keeps the exporter untouched.

    It walks the same statement builder inside one REPEATABLE READ transaction
    and depends on path order; nothing about that changed, because nothing
    about the default did.
    """

    _stamp(corpus, "updated_at", {"alpha": datetime(2026, 9, 3, tzinfo=UTC)})

    default = _list(client, read_only_token, limit=100)
    explicit = _list(client, read_only_token, sort="path", limit=100)

    assert _paths(default, corpus) == _paths(explicit, corpus)
    paths = [note["vault_path"] for note in default["notes"]]
    assert paths == sorted(paths)


def test_an_unknown_order_is_refused(
    client: TestClient,
    read_only_token: str,
) -> None:
    """The set is closed, so `recent` is a typo -- and answering a typo with
    the default order answers a question nobody asked."""

    response = client.get(
        "/api/v1/vault/notes",
        headers={"Authorization": f"Bearer {read_only_token}"},
        params={"sort": "recent"},
    )

    assert response.status_code == 422


def test_every_order_names_a_column_rather_than_describing_one() -> None:
    """The invariant AGENTS.md states for `sort_order` and `period`.

    A request names a member of a closed enum and the enum names a column the
    repository holds, so nothing a caller sends is interpolated. That is a
    property of the mapping rather than of any one request, so it is asserted
    on the mapping -- including that the mapping is total, since a member with
    no entry would turn a valid request into a `KeyError`.
    """

    assert set(SORT_KEYS) == set(NoteSort)

    listed = {column.name for column in DOCUMENT_BRIEF_COLUMNS}
    for sort in NoteSort:
        column, _descending = SORT_KEYS[sort]
        assert column.table is vault_documents
        assert column.name in listed, (
            "an order must be by a column the listing actually reads, or a "
            "row cannot say which order it is in"
        )


def test_a_listing_row_carries_no_body(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Discovery and retrieval have separate costs.

    The same decision `VaultSearchHit` records: a page of rows a caller mostly
    discards must not carry the bodies they would have discarded.
    """

    payload = _list(client, read_only_token, path="Human/03 Projects/", limit=100)
    row = payload["notes"][0]

    assert "body" not in row
    assert set(row) == {
        "note_id",
        "title",
        "vault_path",
        "kind",
        "status",
        "doc_type",
        "doc_status",
        "summary",
        "updated_at",
        # Carried so a listing ordered by either timestamp can show which one
        # it is ordered by, and because "what is new" and "what changed" are
        # different questions (ADR 0045).
        "created_at",
        "content_revision",
    }


def test_a_listing_row_carries_both_timestamps(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Not interchangeable, and both published.

    `updated_at` is the curated one -- `set_status` and `set_promotion_status`
    deliberately leave it where it is -- so it means an author changed the
    note rather than that something touched the row. `created_at` never moves
    at all. A listing that can be ordered by either has to be able to show
    which order it is in.
    """

    row = _list(client, read_only_token, path="Human/03 Projects/", limit=1)[
        "notes"
    ][0]

    created = datetime.fromisoformat(row["created_at"])
    updated = datetime.fromisoformat(row["updated_at"])

    assert created.tzinfo is not None, "a naive timestamp cannot be ordered"
    assert updated.tzinfo is not None
    # Seeded in one transaction, so `now()` gives both the same value. What is
    # asserted is that the row publishes each of them, not a gap between.
    assert created <= updated


def test_a_note_without_a_summary_lists_with_a_null_one(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Ordinary rather than an error: not every note carries a precis."""

    payload = _list(client, read_only_token, path="Human/04 Areas/", limit=100)
    row = next(
        note for note in payload["notes"] if note["note_id"] == corpus["gamma"]
    )

    assert row["summary"] is None


def test_a_listing_never_asks_postgres_for_a_body() -> None:
    """The body-free contract as a query, not as a projection.

    The endpoint published summaries while the repository selected the whole
    row -- body, frontmatter, provenance, every array -- and dropped the rest
    on the way out. A page of a hundred large notes therefore cost megabytes
    off the wire to render a list of titles.

    Compiled rather than described: the SQL itself is the claim.
    """

    statement = path_page_statement(DOCUMENT_BRIEF_COLUMNS, ("Human/",), limit=100)
    sql = str(statement.compile())

    assert "vault_documents.title" in sql
    for heavy in ("vault_documents.body", "vault_documents.provenance",
                  "vault_documents.frontmatter", "vault_documents.aliases"):
        assert heavy not in sql, f"a listing must not select {heavy}"


def test_the_brief_record_cannot_carry_a_body() -> None:
    """A partially-filled `VaultDocument` would keep the promise in the
    response and break it in the query, which is the defect this closes."""

    assert not hasattr(VaultDocumentBrief, "body")
    assert set(VaultDocumentBrief.__slots__) == {
        column.name for column in DOCUMENT_BRIEF_COLUMNS
    }


def test_both_listings_page_by_the_same_rules() -> None:
    """Same filters, same order, same cursor -- one function builds both.

    Two listings over one corpus that paged differently would be a bug nobody
    could see from either one alone.
    """

    prefixes = ("Human/",)
    arguments = {
        "after": ("Human/03 Projects/alpha.md", "note-id"),
        "limit": 7,
        "statuses": (DocumentStatus.ACTIVE,),
        "readable_only": True,
        "tags": ("hss",),
        "facets": {"project": ["hss"]},
    }
    brief = str(
        path_page_statement(DOCUMENT_BRIEF_COLUMNS, prefixes, **arguments).compile()
    )
    full = str(
        path_page_statement(DOCUMENT_DOMAIN_COLUMNS, prefixes, **arguments).compile()
    )

    assert brief[brief.index("FROM"):] == full[full.index("FROM"):]


# ── Edge resolution ─────────────────────────────────────────────────────────


def _edges(client: TestClient, token: str, ids: list[str]) -> dict:
    response = client.post(
        "/api/v1/vault/notes/edges",
        headers={"Authorization": f"Bearer {token}"},
        json={"ids": ids},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_edges_resolve_to_the_slug_a_wikilink_would_carry(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """`related_ids` hold ids; a person reading them needs the name.

    ADR 0025 keeps edges as ids inside the system, so a surface that shows
    them to a human has to resolve them. The slug is the leaf of `vault_path`,
    which is what the export writes as `[[slug]]` -- so a link in the console
    and a link in the exported tree name the same note identically.
    """

    payload = _edges(client, read_only_token, [corpus["alpha"]])

    assert len(payload["edges"]) == 1
    edge = payload["edges"][0]
    assert edge["note_id"] == corpus["alpha"]
    assert edge["title"] == "Listing fixture alpha"
    # The vault_path is Human/03 Projects/alpha-<run>.md, so the slug is its
    # stem -- never the uuid, and never the full path.
    assert edge["slug"].startswith("alpha-")
    assert "/" not in edge["slug"]
    assert not edge["slug"].endswith(".md")


def test_edge_resolution_withholds_what_the_read_policy_withholds(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """Resolving an id to a title says the note exists and what it is called.

    That is the disclosure `find_similar` already filters for, so this filters
    the same way. Archived resolves -- retired history a reference may still
    legitimately point at, exactly as fetch-by-id treats it. Flagged and
    out-of-policy do not, and neither is reported as a distinct outcome: an
    absent id covers "no such note" and "not yours to read" alike, because
    telling them apart confirms the id exists.
    """

    requested = [corpus[name] for name in ("alpha", "delta", "epsilon", "zeta")]
    payload = _edges(client, read_only_token, requested)

    resolved = {edge["note_id"] for edge in payload["edges"]}
    assert corpus["alpha"] in resolved
    assert corpus["delta"] in resolved, "archived is readable and must resolve"
    assert corpus["epsilon"] not in resolved, "flagged must not resolve"
    assert corpus["zeta"] not in resolved, "outside the read policy must not resolve"


def test_an_unresolvable_edge_is_absent_rather_than_an_error(
    client: TestClient,
    read_only_token: str,
    corpus: dict[str, str],
) -> None:
    """A dangling edge is ordinary, not a failure.

    `related_ids` carry no existence check on purpose (ADR 0030), so an id
    naming nothing is a state the corpus is expected to be in. The caller
    renders the bare id; a 404 here would make one dead edge break the whole
    resolution for a note.
    """

    payload = _edges(
        client, read_only_token, [corpus["alpha"], "no-such-document-at-all"]
    )

    assert [edge["note_id"] for edge in payload["edges"]] == [corpus["alpha"]]


def test_edge_resolution_requires_the_read_scope(client: TestClient) -> None:
    credential_id, token = _issue(scopes=(VaultScope.PROPOSE,))
    try:
        response = client.post(
            "/api/v1/vault/notes/edges",
            headers={"Authorization": f"Bearer {token}"},
            json={"ids": ["anything"]},
        )
    finally:
        _drop(credential_id)

    assert response.status_code == 403


def test_edge_resolution_does_not_collide_with_fetch_by_id(
    client: TestClient,
    read_only_token: str,
) -> None:
    """`/notes/edges` is declared before `/notes/{note_id}`, and must stay so.

    Route order is what stops FastAPI reading "edges" as a note id. If the two
    are ever reordered this returns 404 from the fetch handler instead, which
    is why the assertion is on the shape rather than merely on the status.
    """

    response = client.post(
        "/api/v1/vault/notes/edges",
        headers={"Authorization": f"Bearer {read_only_token}"},
        json={"ids": []},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"edges": []}


def test_edge_lookup_refuses_more_ids_than_it_will_resolve(
    client: TestClient,
    read_only_token: str,
) -> None:
    """The bound is declared so the console can batch to it."""

    over = [f"{index:032x}" for index in range(MAX_EDGE_LOOKUP_IDS + 1)]

    response = client.post(
        "/api/v1/vault/notes/edges",
        headers={"Authorization": f"Bearer {read_only_token}"},
        json={"ids": over},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "too_long"


def test_edge_lookup_survives_ids_too_long_for_a_request_line(
    client: TestClient,
    corpus: dict[str, str],
    read_only_token: str,
) -> None:
    """A full batch of long ids used to die before reaching the app.

    Nothing bounds an id's length -- `validate_edge_ids` checks shape and
    uniqueness, not size -- so a hundred of them in a query string can exceed
    the 8,192-byte request line Heroku's router accepts. The request then
    fails at the router, which the console reports as a lookup failure, and
    every edge on the note reads "not looked up".

    A body has no such ceiling. The payload here is far past that limit and
    still resolves the one real id among the long ones.
    """

    padding = ["x" * 400 for _ in range(MAX_EDGE_LOOKUP_IDS - 1)]
    long_ids = [f"{index}-{value}" for index, value in enumerate(padding)]
    ids = [corpus["alpha"], *long_ids]

    assert sum(len(value) + 4 for value in ids) > 8192, (
        "the fixture must exceed the request-line limit it is testing"
    )

    payload = _edges(client, read_only_token, ids)

    assert [edge["note_id"] for edge in payload["edges"]] == [corpus["alpha"]]
