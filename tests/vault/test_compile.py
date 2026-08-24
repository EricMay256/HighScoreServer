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
from sqlalchemy import delete, select, text, update

from app.vault.auth import VaultScope
from app.vault.domain import (
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    NoteCompileState,
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
from tests.vault.test_contributions import StubEmbeddingProvider
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


def _note(
    updated_at: datetime = COMPILED_AT,
    status: str = "active",
    declined_at: datetime | None = None,
) -> NoteCompileState:
    return NoteCompileState(
        updated_at=updated_at, status=status, declined_at=declined_at
    )


def _notes(**overrides: NoteCompileState) -> dict[str, NoteCompileState]:
    base = {"note-1": _note(COMPILED_AT - timedelta(hours=1))}
    base.update(overrides)
    return base


def test_a_page_whose_sources_have_not_moved_is_not_replanned() -> None:
    items = _plan_items(pages=[_page()], notes=_notes(), all_pages=False)

    assert items == ()


def test_a_source_edited_after_compilation_makes_a_page_stale() -> None:
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-1": _note(COMPILED_AT + timedelta(hours=1))}),
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
        notes=_notes(
            **{"note-1": _note(COMPILED_AT - timedelta(hours=1), "flagged")}
        ),
        all_pages=False,
    )

    assert [item.reason for item in items] == ["stale"]


def test_a_source_that_no_longer_exists_is_missing_not_stale() -> None:
    """The distinction matters: the page makes a provenance claim that is false."""

    items = _plan_items(
        pages=[_page(source_ids=("note-1", "note-gone"))],
        notes=_notes(),
        all_pages=False,
    )

    assert [item.reason for item in items] == ["missing"]


def test_a_note_no_page_covers_is_a_new_source() -> None:
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-2": _note(COMPILED_AT)}),
        all_pages=False,
    )

    assert [(i.reason, i.source_ids) for i in items] == [
        ("new-source", ("note-2",))
    ]


def test_a_declined_note_is_not_re_offered() -> None:
    """Otherwise the plan becomes a backlog nobody can clear.

    A note a compiler looked at and refused should stay refused. Offering it
    again every run makes the plan permanently non-empty, which is the state in
    which nobody reads it.
    """

    items = _plan_items(
        pages=[],
        notes={
            "note-2": _note(
                COMPILED_AT - timedelta(days=1), declined_at=COMPILED_AT
            )
        },
        all_pages=False,
    )

    assert items == ()


def test_a_note_edited_after_being_declined_is_offered_again() -> None:
    """A note that changed since the judgement is a different note.

    The frontier gave this for free, because an edit moved the note past the
    bookmark by construction. An explicit decline has to say it, and this is
    where it is said.
    """

    items = _plan_items(
        pages=[],
        notes={
            "note-2": _note(
                COMPILED_AT + timedelta(hours=1), declined_at=COMPILED_AT
            )
        },
        all_pages=False,
    )

    assert [i.reason for i in items] == ["new-source"]


def test_an_undeclined_note_is_offered_however_old_it_is() -> None:
    """The case the frontier could not express, and the reason it was replaced.

    Age is not a judgement. A note nobody was ever shown -- because it was
    flagged when the only run happened, say -- has to keep being offered
    however far in the past it sits. Under a frontier this note was
    indistinguishable from one that had been considered and refused.
    """

    ancient = COMPILED_AT - timedelta(days=365)
    items = _plan_items(
        pages=[], notes={"note-2": _note(ancient)}, all_pages=False
    )

    assert [i.reason for i in items] == ["new-source"]


def test_all_pages_replans_everything_including_declined_notes() -> None:
    """The full flush an operator wants after changing the page model.

    It is also the recovery path when a decline turns out to have been wrong,
    which is why it has to ignore them rather than merely ignore coverage.
    """

    old = COMPILED_AT - timedelta(days=1)
    items = _plan_items(
        pages=[_page()],
        notes=_notes(**{"note-2": _note(old, declined_at=COMPILED_AT)}),
        all_pages=True,
    )

    assert sorted(i.reason for i in items) == ["new-source", "stale"]


def test_a_flagged_note_is_never_offered_as_a_new_source() -> None:
    """Compiling unendorsed content into the wiki would launder it."""

    items = _plan_items(
        pages=[],
        notes={"note-2": _note(COMPILED_AT, "flagged")},
        all_pages=False,
    )

    assert items == ()


# --------------------------------------------------------- over the wire ----


@pytest.fixture(autouse=True)
def provider(monkeypatch: pytest.MonkeyPatch) -> StubEmbeddingProvider:
    """A deterministic embedding provider, for two independent reasons.

    **CI has no credential**, deliberately: the provider is optional at runtime
    and the vault degrades to lexical search without one. Every test here that
    writes a page would otherwise get a 503, which is correct behaviour and a
    useless assertion -- and it passes locally, where a key is configured, so
    the failure only ever appears on a pull request.

    **The stub is also the stronger tool.** Its vectors are one-hot on an axis
    derived from the text, so identical text scores exactly 1.0 rather than
    approximately -- which is what the dedup assertions below actually need at
    `flag_at = 1.0`. Leaning on a real model for that was measuring the model.

    Autouse because every test in the "over the wire" section writes a page,
    and a test that wants the *unconfigured* path should patch it back to None
    explicitly rather than rely on the environment.
    """

    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: stub)
    return stub


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


# ------------------------------------------ frontier and settle ordering ----


def test_a_note_created_during_a_run_is_still_offered_afterwards(
    client: TestClient, compile_token: str
) -> None:
    """The frontier bug, and it lost notes permanently.

    `finish` used to publish a *fresh* maximum `updated_at` rather than the one
    the run was planned against. A note created after planning was never in the
    plan, so the compiler never saw it -- and the finish-time maximum then
    covered its timestamp, so every later incremental plan skipped it. It could
    never be compiled.

    Publishing the plan-time frontier instead means the next plan reconsiders
    everything written since planning began, which is the harmless direction: a
    page that does cover the note removes it from `new-source` anyway.
    """

    _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    # Lands after the plan, so this run neither saw it nor compiled it.
    missed = _seed_note(title="Written while the run was open")

    client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )
    following = _plan(client, compile_token).json()

    offered = [i for i in following["items"] if i["source_ids"] == [missed]]
    assert [i["reason"] for i in offered] == ["new-source"]


def test_a_successful_run_publishes_the_frontier_it_planned_against(
    client: TestClient, compile_token: str
) -> None:
    _seed_note()
    planned = _plan(client, compile_token).json()["run"]
    run_id = planned["run_id"]

    settled = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    ).json()

    assert settled["output_frontier"] == planned["input_frontier"]


def test_a_page_is_refused_when_a_source_vanishes_before_the_write(
    client: TestClient, compile_token: str
) -> None:
    """Sources are validated before the embedding call, which takes seconds.

    A retirement landing inside that window used to go unnoticed, storing a page
    whose provenance named a note that no longer existed -- the one thing
    `source_ids` validation exists to prevent. Re-checked under the corpus lock
    now, which retirement also takes.

    Simulated by deleting the note between the plan and the write, which is the
    same state the race produces.
    """

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    transactions, engine = vault_service()

    async def remove() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(vault_documents.c.id == note_id)
                )
        finally:
            await engine.dispose()

    asyncio.run(remove())

    response = _write(client, compile_token, run_id, source_ids=[note_id])

    assert response.status_code == 422
    assert "unresolved source id" in response.json()["detail"]
    assert _page_count() == 0


def test_settling_takes_the_corpus_lock() -> None:
    """What stops a page committing into a run that has already settled.

    `write_page` re-checks the run state under the corpus advisory lock, but
    that check only guards anything if the settle path contends for the same
    lock -- otherwise `finish` can commit in the window between the check and
    the insert, and the page lands attributed to a finished run.

    Asserted on the source rather than by racing two transactions: the property
    is "these three take the same lock", and a timing test for it would be
    slow and flaky while proving less.
    """

    import inspect

    from app.vault.service import VaultCompileService

    for method in (
        VaultCompileService.write_page,
        VaultCompileService.finish,
        VaultCompileService.fail,
    ):
        source = inspect.getsource(method)
        assert "pg_advisory_xact_lock" in source, method.__name__


def _note_title(note_id: str) -> str:
    transactions, engine = vault_service()

    async def read() -> str:
        try:
            async with transactions.transaction() as connection:
                result = await connection.execute(
                    select(vault_documents.c.title).where(
                        vault_documents.c.id == note_id
                    )
                )
                return result.scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_a_page_id_naming_a_note_is_refused_before_anything_is_written(
    client: TestClient, compile_token: str
) -> None:
    """A stale or mistyped id used to reach an assertion, not a response.

    `replace_content` matches on id alone, which is right for it -- the ordinary
    update path edits notes through it. `set_compile_provenance` is wiki-only.
    So a note id here overwrote the note with page content, then found no row to
    stamp, then failed an `assert` and returned 500. The transaction rolled the
    write back, but a bad field value in a request any `vault:compile` credential
    can send is not a server fault, and an `assert` is not an error path -- `-O`
    strips it, and the next line would fail on None instead.
    """

    note_id = _seed_note(title="A note, not a page")
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = _write(
        client, compile_token, run_id, page_id=note_id, source_ids=[note_id]
    )

    assert response.status_code == 422
    assert "not a wiki page" in response.json()["detail"]
    assert _note_title(note_id) == "A note, not a page"
    assert _page_count() == 0


def test_an_unknown_page_id_is_still_a_404(
    client: TestClient, compile_token: str
) -> None:
    """The kind check must not swallow the missing-row case."""

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = _write(
        client,
        compile_token,
        run_id,
        page_id=f"{PREFIX}{uuid4().hex}",
        source_ids=[note_id],
    )

    assert response.status_code == 404


def test_a_run_cannot_be_written_by_another_principal(
    client: TestClient, compile_token: str
) -> None:
    """`compiler_principal_id` was recorded and never checked.

    A run that names one compiler while its pages and settlement events name
    another is provenance contradicting itself, which is the one thing a compile
    run exists to provide. Not an authorization boundary -- the scope already
    permits writing wiki pages, and the caller can open its own run -- so the
    refusal is 409.
    """

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    other_id, other_token = _issue(
        scopes=(VaultScope.READ, VaultScope.WRITE, VaultScope.COMPILE)
    )
    try:
        response = _write(
            client, other_token, run_id, source_ids=[note_id]
        )
        assert response.status_code == 409
        assert "different principal" in response.json()["detail"]
        assert _page_count() == 0
    finally:
        _drop(other_id)


def test_a_run_cannot_be_settled_by_another_principal(
    client: TestClient, compile_token: str
) -> None:
    """Settling publishes a frontier on the opener's behalf.

    The frontier is what stops notes being re-offered, so finishing someone
    else's run silently narrows their next plan.
    """

    _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    other_id, other_token = _issue(
        scopes=(VaultScope.READ, VaultScope.WRITE, VaultScope.COMPILE)
    )
    try:
        finished = client.post(
            f"/api/v1/vault/compile/runs/{run_id}/finish",
            headers=_auth(other_token),
        )
        failed = client.post(
            f"/api/v1/vault/compile/runs/{run_id}/fail",
            json={"error_summary": "not mine to fail"},
            headers=_auth(other_token),
        )
        assert finished.status_code == 409
        assert failed.status_code == 409
    finally:
        _drop(other_id)

    # Still open, and still the opener's to settle.
    mine = client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )
    assert mine.status_code == 200


# ------------------------------------------------------- declining a note ----


def _decline(client: TestClient, token: str, run_id: str, note_ids: list[str]):
    return client.post(
        f"/api/v1/vault/compile/runs/{run_id}/declines",
        json={"note_ids": note_ids},
        headers=_auth(token),
    )


def _set_status(note_id: str, status: DocumentStatus, doc_status: str) -> None:
    transactions, engine = vault_service()

    async def go() -> None:
        try:
            async with transactions.transaction() as connection:
                await VaultDocumentRepository().set_status(
                    connection, note_id, status=status, doc_status=doc_status
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def _touch(note_id: str) -> None:
    """Move a note's `updated_at` without changing anything a reader sees."""

    transactions, engine = vault_service()

    async def go() -> None:
        try:
            async with transactions.transaction() as connection:
                await connection.execute(
                    update(vault_documents)
                    .where(vault_documents.c.id == note_id)
                    .values(updated_at=text("now() + interval '1 second'"))
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def _offered(client: TestClient, token: str, note_id: str) -> bool:
    plan = _plan(client, token).json()
    return any(item["source_ids"] == [note_id] for item in plan["items"])


def test_declining_a_note_stops_it_being_offered(
    client: TestClient, compile_token: str
) -> None:
    note_id = _seed_note(title="Not worth a page")
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    assert _offered(client, compile_token, note_id)

    response = _decline(client, compile_token, run_id, [note_id])

    assert response.status_code == 200
    assert response.json()["declined_note_ids"] == [note_id]
    assert not _offered(client, compile_token, note_id)


def test_a_declined_note_is_offered_again_once_it_changes(
    client: TestClient, compile_token: str
) -> None:
    """The decline is about the note as it stood, not the note forever."""

    note_id = _seed_note(title="Declined, then rewritten")
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    _decline(client, compile_token, run_id, [note_id])
    assert not _offered(client, compile_token, note_id)

    _touch(note_id)

    assert _offered(client, compile_token, note_id)


def test_all_pages_ignores_declines(
    client: TestClient, compile_token: str
) -> None:
    """The recovery path when a decline was wrong."""

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    _decline(client, compile_token, run_id, [note_id])

    plan = _plan(client, compile_token, all_pages=True).json()

    assert any(item["source_ids"] == [note_id] for item in plan["items"])


def test_a_flagged_note_approved_later_is_still_offered(
    client: TestClient, compile_token: str
) -> None:
    """The bug the frontier caused, reachable through the ordinary review flow.

    A flagged note is deliberately never offered as a new source -- but it used
    to count toward the frontier all the same, because that was
    `max(updated_at)` across every note whatever its status. And `set_status`
    deliberately does not move `updated_at`, since adjudicating a note is not
    editing it. So: flagged, a run succeeds and publishes a frontier past it,
    a reviewer approves it, and no incremental plan ever offers it again.

    No misbehaviour anywhere -- two correct decisions and a timestamp standing
    in for a judgement. Declines cannot express it: nobody declined this note,
    so nothing suppresses it.
    """

    note_id = _seed_note(title="Flagged at plan time, approved later")
    _set_status(note_id, DocumentStatus.FLAGGED, "Flagged")

    # A run happens while it is flagged. It is correctly not offered.
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    assert not _offered(client, compile_token, note_id)
    client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )

    _set_status(note_id, DocumentStatus.ACTIVE, "Active")

    assert _offered(client, compile_token, note_id)


def test_declining_an_unknown_id_is_refused(
    client: TestClient, compile_token: str
) -> None:
    """Silently dropping it would leave the note offered forever, unexplained."""

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    response = _decline(
        client, compile_token, run_id, [note_id, f"{PREFIX}{uuid4().hex}"]
    )

    assert response.status_code == 422
    assert "unresolved source id" in response.json()["detail"]
    # Refused as a whole: the good id is not declined either.
    assert _offered(client, compile_token, note_id)


def test_a_wiki_page_id_cannot_be_declined(
    client: TestClient, compile_token: str
) -> None:
    """Declining is refusing to write a page *from a note*.

    The repository filters on `kind` rather than trusting the caller, so a page
    id resolves to nothing and comes back a 422 -- not a constraint violation
    and not a silently ignored request.
    """

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    page = _write(client, compile_token, run_id, source_ids=[note_id])
    assert page.status_code == 201, page.text
    page_id = page.json()["note_id"]

    response = _decline(client, compile_token, run_id, [page_id])

    assert response.status_code == 422


def test_a_run_cannot_decline_for_another_principal(
    client: TestClient, compile_token: str
) -> None:
    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]

    other_id, other_token = _issue(
        scopes=(VaultScope.READ, VaultScope.WRITE, VaultScope.COMPILE)
    )
    try:
        response = _decline(client, other_token, run_id, [note_id])
        assert response.status_code == 409
    finally:
        _drop(other_id)


def test_a_settled_run_cannot_decline(
    client: TestClient, compile_token: str
) -> None:
    """A judgement must not attach to a run that has reported its result."""

    note_id = _seed_note()
    run_id = _plan(client, compile_token).json()["run"]["run_id"]
    client.post(
        f"/api/v1/vault/compile/runs/{run_id}/finish", headers=_auth(compile_token)
    )

    response = _decline(client, compile_token, run_id, [note_id])

    assert response.status_code == 409


def test_declining_requires_the_compile_scope(client: TestClient) -> None:
    note_id = _seed_note()
    writer_id, writer_token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        response = _decline(client, writer_token, str(uuid4()), [note_id])
        assert response.status_code == 403
    finally:
        _drop(writer_id)
