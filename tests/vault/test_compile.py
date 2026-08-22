"""Wiki compilation: planning a run, writing its pages, settling it.

The last markdown writer. `Agent/wiki/` was produced by the Stage-A librarian
loop because the service had no compile path; this is that path.

Two levels, for the reason `test_export.py` uses two. `_plan_items` is pure and
tested directly, because the three staleness rules are the part most likely to
be got subtly wrong and they need no database to state. Everything else runs
over HTTP against the real schema, because compile provenance is a foreign key
with `ON DELETE RESTRICT` and a CHECK that admits no partial state -- facts
about Postgres, not about Python.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.vault.auth import VaultScope
from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    VaultDocument,
)
from app.vault.repository import VaultDocumentRepository
from app.vault.service import AGENT_WIKI_DIRECTORY, _plan_items
from app.vault.settings import vault_enabled
from app.vault.tables import (
    vault_audit_events,
    vault_compile_runs,
    vault_documents,
    vault_write_requests,
)
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

PREFIX = "test-compile-"
# A contributed note is stored with a service-assigned id carrying no test
# prefix, so cleanup has to find it by title.
TWIN_TITLE_PREFIX = "Compiled twin "
COMPILED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


# ------------------------------------------------- the planner, in memory ----


def _page(
    page_id: str = "page-1",
    *,
    source_ids: tuple[str, ...] = ("note-1",),
    compiled_at: datetime = COMPILED_AT,
) -> VaultDocument:
    return VaultDocument(
        id=page_id,
        kind=DocumentKind.WIKI,
        status=DocumentStatus.ACTIVE,
        vault_path=f"{AGENT_WIKI_DIRECTORY}{page_id}.md",
        title=f"Page {page_id}",
        body="Synthesis.",
        contributed_by="agent:librarian",
        provenance={},
        schema_version=1,
        created_at=COMPILED_AT,
        updated_at=COMPILED_AT,
        source_ids=source_ids,
        compiled_at=compiled_at,
    )


def _notes(**overrides: tuple[datetime, str]) -> dict[str, tuple[datetime, str]]:
    base = {"note-1": (COMPILED_AT - timedelta(hours=1), "active")}
    base.update(overrides)
    return base


def test_a_page_whose_sources_have_not_moved_is_not_replanned() -> None:
    items = _plan_items(pages=[_page()], notes=_notes(), since=None, all_pages=False)

    assert items == ()


def test_a_source_edited_after_compilation_makes_a_page_stale() -> None:
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-1": (COMPILED_AT + timedelta(hours=1), "active")}),
        since=None,
        all_pages=False,
    )

    assert [item.reason for item in items] == ["stale"]
    assert items[0].page_id == "page-1"


def test_a_source_that_has_since_been_flagged_makes_a_page_stale() -> None:
    """One of the three Stage-A reasons, and the one a status query would miss.

    The page still cites a note the write path declined to endorse, so the
    synthesis rests on something the read surface now withholds.
    """

    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-1": (COMPILED_AT - timedelta(hours=1), "flagged")}),
        since=None,
        all_pages=False,
    )

    assert [item.reason for item in items] == ["stale"]


def test_a_source_that_no_longer_exists_is_missing_not_stale() -> None:
    """The distinction matters: the page makes a provenance claim that is false."""

    items = _plan_items(
        pages=[_page(source_ids=("note-1", "note-gone"))],
        notes=_notes(),
        since=None,
        all_pages=False,
    )

    assert [item.reason for item in items] == ["missing"]


def test_a_note_no_page_covers_is_a_new_source() -> None:
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-2": (COMPILED_AT, "active")}),
        since=None,
        all_pages=False,
    )

    assert [(i.reason, i.source_ids) for i in items] == [
        ("new-source", ("note-2",))
    ]


def test_an_uncovered_note_older_than_the_frontier_is_not_re_offered() -> None:
    """Otherwise the plan becomes a backlog nobody can clear.

    A note older than the last successful run's frontier and still uncovered
    was either deliberately left out or already offered and declined. Offering
    it again every run makes the plan permanently non-empty.
    """

    old = COMPILED_AT - timedelta(days=1)
    items = _plan_items(
        pages=[],
        notes={"note-2": (old, "active")},
        since=COMPILED_AT.isoformat(),
        all_pages=False,
    )

    assert items == ()


def test_a_note_written_after_the_frontier_is_offered() -> None:
    fresh = COMPILED_AT + timedelta(hours=1)
    items = _plan_items(
        pages=[],
        notes={"note-2": (fresh, "active")},
        since=COMPILED_AT.isoformat(),
        all_pages=False,
    )

    assert [i.reason for i in items] == ["new-source"]


def test_all_pages_replans_everything_regardless_of_frontier() -> None:
    """The full flush an operator wants after changing the page model."""

    old = COMPILED_AT - timedelta(days=1)
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-2": (old, "active")}),
        since=COMPILED_AT.isoformat(),
        all_pages=True,
    )

    assert sorted(i.reason for i in items) == ["new-source", "stale"]


def test_a_flagged_note_is_never_offered_as_a_new_source() -> None:
    """Compiling unendorsed content into the wiki would launder it."""

    items = _plan_items(
        pages=[],
        notes={"note-2": (COMPILED_AT, "flagged")},
        since=None,
        all_pages=False,
    )

    assert items == ()


# --------------------------------------------------------- over the wire ----


@pytest.fixture
def compile_token(configure_test_env: None):
    credential_id, token = _issue(
        scopes=(VaultScope.READ, VaultScope.WRITE, VaultScope.COMPILE)
    )
    try:
        yield token
    finally:
        _drop(credential_id)


def _seed_note(title: str = "A note worth synthesizing") -> str:
    note_id = f"{PREFIX}{uuid4().hex}"
    transactions, engine = vault_service()

    async def seed() -> None:
        try:
            async with transactions.transaction() as connection:
                await VaultDocumentRepository().insert(
                    connection,
                    NewVaultDocument(
                        id=note_id,
                        kind=DocumentKind.NOTE,
                        doc_type="Agent Note",
                        vault_path=f"Agent/notes/{note_id}.md",
                        status=DocumentStatus.ACTIVE,
                        doc_status="Active",
                        title=title,
                        body="Something the librarian will distil.",
                        contributed_by=f"agent:{PREFIX}seed",
                        provenance={"fixture": True},
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    return note_id


def _cleanup() -> None:
    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                # Pages first: compile_run_id is ON DELETE RESTRICT, so a run
                # cannot be removed while anything cites it.
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.kind == DocumentKind.WIKI.value
                    )
                )
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.like(f"{PREFIX}%")
                    )
                )
                # The write ledger references the contributed note, so its
                # row goes first -- vault_write_requests.document_id is a real
                # foreign key, unlike the correlation identifiers on audit
                # events (ADR 0002).
                twins = (
                    select(vault_documents.c.id)
                    .where(vault_documents.c.title.like(f"{TWIN_TITLE_PREFIX}%"))
                    .scalar_subquery()
                )
                await connection.execute(
                    delete(vault_write_requests).where(
                        vault_write_requests.c.document_id.in_(twins)
                    )
                )
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.title.like(f"{TWIN_TITLE_PREFIX}%")
                    )
                )
                await connection.execute(delete(vault_compile_runs))
                await connection.execute(
                    delete(vault_audit_events).where(
                        vault_audit_events.c.operation == "vault.compile"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())


@pytest.fixture(autouse=True)
def clean_compile_state(configure_test_env: None):
    _cleanup()
    yield
    _cleanup()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _plan(client: TestClient, token: str, all_pages: bool = False):
    return client.post(
        "/api/v1/vault/compile/runs",
        params={"all_pages": all_pages},
        headers=_auth(token),
    )


def _write(client: TestClient, token: str, run_id: str, **body):
    payload = {
        "title": "Idempotency across the write path",
        "body": "A synthesis of what the notes say.",
        "source_ids": [],
    }
    payload.update(body)
    return client.post(
        f"/api/v1/vault/compile/runs/{run_id}/pages",
        json=payload,
        headers=_auth(token),
    )


def test_compile_requires_its_own_scope(client: TestClient) -> None:
    """`vault:compile` is a verb of its own (ADR 0020); read+write is not it."""

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        response = _plan(client, token)
    finally:
        _drop(credential_id)

    assert response.status_code == 403


def test_a_plan_offers_an_uncovered_note_and_opens_a_running_run(
    client: TestClient, compile_token: str
) -> None:
    note_id = _seed_note()

    response = _plan(client, compile_token)
    payload = response.json()

    assert response.status_code == 201
    assert payload["run"]["state"] == "running"
    assert payload["run"]["completed_at"] is None
    offered = [i for i in payload["items"] if i["source_ids"] == [note_id]]
    assert [i["reason"] for i in offered] == ["new-source"]


def test_a_plan_carries_ids_and_never_note_bodies(
    client: TestClient, compile_token: str
) -> None:
    """The agent fetches content through the policy-checked read surface.

    Inlining bodies here would be a second read path with its own disclosure
    rules, and a response the size of the corpus.
    """

    _seed_note(title="A note whose body must not appear in the plan")

    payload = _plan(client, compile_token).json()

    assert "Something the librarian will distil." not in response_text(payload)


def response_text(payload: object) -> str:
    import json

    return json.dumps(payload)


def test_writing_a_page_stores_it_with_its_run_as_provenance(
    client: TestClient, compile_token: str
) -> None:
    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = _write(client, compile_token, run_id, source_ids=[note_id])
    page = response.json()

    assert response.status_code == 201
    assert page["kind"] == "wiki"
    assert page["source_ids"] == [note_id]
    assert page["vault_path"].startswith(AGENT_WIKI_DIRECTORY)
    assert page["vault_path"].endswith("idempotency-across-the-write-path.md")


def test_a_page_citing_an_unresolved_note_is_refused(
    client: TestClient, compile_token: str
) -> None:
    """Unlike a note's related_ids, which ADR 0025 keeps opaque on purpose.

    `source_ids` is provenance. Naming something that never existed is a false
    claim rather than a dangling edge.
    """

    _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = _write(
        client, compile_token, run_id, source_ids=[f"{PREFIX}{uuid4().hex}"]
    )

    assert response.status_code == 422
    assert "unresolved source id" in response.json()["detail"]
    assert _page_count() == 0


def test_a_page_cannot_be_written_into_a_settled_run(
    client: TestClient, compile_token: str
) -> None:
    """Its provenance would name a run that had already reported its output."""

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )

    response = _write(client, compile_token, run_id, source_ids=[note_id])

    assert response.status_code == 409


def test_an_unknown_run_is_a_404(client: TestClient, compile_token: str) -> None:
    note_id = _seed_note()

    response = _write(client, compile_token, str(uuid4()), source_ids=[note_id])

    assert response.status_code == 404


def test_finishing_publishes_a_frontier_and_settles_the_run(
    client: TestClient, compile_token: str
) -> None:
    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    _write(client, compile_token, run_id, source_ids=[note_id])

    response = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )
    run = response.json()

    assert response.status_code == 200
    assert run["state"] == "succeeded"
    assert run["completed_at"] is not None
    assert run["output_frontier"]["frontier_at"]


def test_finishing_twice_is_a_conflict(
    client: TestClient, compile_token: str
) -> None:
    """The state predicate is the concurrency guard, as with a review decision."""

    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    first = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )
    second = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_a_failed_run_keeps_its_pages_and_publishes_no_frontier(
    client: TestClient, compile_token: str
) -> None:
    """Not a rollback. The pages are real synthesis with accurate provenance.

    What the failure changes is coverage: no frontier, so the next plan
    re-covers what this run did not finish.
    """

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    _write(client, compile_token, run_id, source_ids=[note_id])

    response = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/fail",
        json={"error_summary": "the model gave up"},
        headers=_auth(compile_token),
    )
    run = response.json()

    assert response.status_code == 200
    assert run["state"] == "failed"
    assert run["output_frontier"] == {}
    assert run["error_summary"] == "the model gave up"
    assert _page_count() == 1


def test_failing_without_a_reason_is_refused(
    client: TestClient, compile_token: str
) -> None:
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/fail",
        json={"error_summary": "   "},
        headers=_auth(compile_token),
    )

    assert response.status_code == 422


def test_a_second_run_does_not_re_offer_a_covered_note(
    client: TestClient, compile_token: str
) -> None:
    """The frontier's whole purpose, end to end."""

    note_id = _seed_note()
    first = _plan(client, compile_token).json()["run"]["run_id"]
    _write(client, compile_token, first, source_ids=[note_id])
    client.post(
        f"/api/v1/vault/compile/runs/{first}/finish", headers=_auth(compile_token)
    )

    second = _plan(client, compile_token).json()

    assert second["items"] == []


def test_a_compiled_page_is_searchable(
    client: TestClient, compile_token: str
) -> None:
    """Pages are embedded even though they skip the dedup gate.

    Search returning synthesis is the point of compiling at all.
    """

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    _write(
        client,
        compile_token,
        run_id,
        source_ids=[note_id],
        title="Distinctive synthesis about idempotency",
    )

    response = client.get(
        "/api/v1/vault/search",
        params={"q": "distinctive synthesis idempotency"},
        headers=_auth(compile_token),
    )
    titles = [hit["title"] for hit in response.json()["hits"]]

    assert response.status_code == 200
    assert "Distinctive synthesis about idempotency" in titles


def test_a_wiki_page_is_not_a_dedup_candidate(
    client: TestClient, compile_token: str
) -> None:
    """A page restates its sources by construction.

    Scoring a note against one conflates two layers -- a page derived from note
    A would make note A's successor look like a duplicate of a document that
    only exists because note A does.

    The page and the contribution carry **byte-identical** text, which under
    `flag_at = 1.0` is the only thing that flags at all. So this asserts the
    filter and nothing weaker: without it the cosine is exactly 1.0 and the
    contribution comes back `flagged`.

    The text is unique per run because the contributed note lands under
    `Agent/notes/` with a service-assigned id that carries no test prefix. A
    fixed string would match the previous run's leftover instead of the page,
    and pass or fail for the wrong reason.
    """

    marker = uuid4().hex
    title = f"{TWIN_TITLE_PREFIX}{marker}"
    body = f"A synthesis carrying the marker {marker}."

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    page = _write(
        client, compile_token, run_id, source_ids=[note_id], title=title, body=body
    )
    assert page.status_code == 201

    response = client.post(
        "/api/v1/vault/contributions",
        json={
            "title": title,
            "body": body,
            "idempotency_key": uuid4().hex,
        },
        headers=_auth(compile_token),
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "inserted"


def test_a_related_page_is_reported_as_context_not_as_a_duplicate(
    client: TestClient, compile_token: str
) -> None:
    """The two purposes one query used to serve, now separated.

    A page near a contribution is worth telling the contributor about -- "there
    is already a synthesis covering this" is actionable. What it must never be
    is the reason a contribution flags, because a page restates its sources by
    construction.

    Byte-identical text again, so the page scores 1.0. Under `flag_at = 1.0`
    that is precisely the score that *would* flag if pages were in the gate's
    corpus, which is what makes this assertion mean something: the same
    document appears in `related_pages` and the outcome is still `inserted`.
    """

    marker = uuid4().hex
    title = f"{TWIN_TITLE_PREFIX}{marker}"
    body = f"A synthesis carrying the marker {marker}."

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    page = _write(
        client, compile_token, run_id, source_ids=[note_id], title=title, body=body
    ).json()

    payload = client.post(
        "/api/v1/vault/contributions",
        json={"title": title, "body": body, "idempotency_key": uuid4().hex},
        headers=_auth(compile_token),
    ).json()

    assert payload["status"] == "inserted"
    reported = {entry["note_id"] for entry in payload["related_pages"]}
    assert page["note_id"] in reported
    # And it is nowhere near the gate's evidence.
    assert page["note_id"] not in {
        entry["note_id"] for entry in payload["similars"]
    }


def _page_count() -> int:
    transactions, engine = vault_service()

    async def run() -> int:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(vault_documents.c.id).where(
                        vault_documents.c.kind == DocumentKind.WIKI.value
                    )
                )
                return len(list(result))
        finally:
            await engine.dispose()

    return asyncio.run(run())
