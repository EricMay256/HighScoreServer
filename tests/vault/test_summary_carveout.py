"""The ADR 0035 carveout: a contributor describing its own recent note.

The interesting assertions here are the refusals, not the success. A carveout
is defined entirely by what it will not do, so each of the four bounds --
one field, empty only, own note only, inside the window -- gets a test that
fails if the bound is removed.

Reuses `test_contributions`' stub provider and cleanup rather than growing a
second copy: the stub is deterministic in text, which is what makes the
re-embedding assertion below mean something. `_issue` mints a fresh principal
per credential, so "another principal" needs no special arrangement.
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.vault.auth import VaultScope
from app.vault.constants import SUMMARY_GRACE_PERIOD_SECONDS
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.settings import vault_enabled
from app.vault.tables import vault_document_embeddings, vault_documents
from tests.vault.test_contributions import (
    PROFILE_ID,
    StubEmbeddingProvider,
    _cleanup,
    _contribute,
    _payload,
)
from tests.vault.test_routes import _drop, _issue
from tests.vault.test_search import vault_service


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)


# Declared here rather than imported. Importing a fixture and then naming it as
# a parameter shadows the import, which ruff reads as a redefinition -- so the
# suite's convention (see `test_amendments`) is that each module owns its
# fixtures and shares only the helpers underneath them.
@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> StubEmbeddingProvider:
    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.routes.get_embedding_provider", lambda: stub)
    return stub


@pytest.fixture
def write_token(configure_test_env: None):
    """Contribute and nothing more.

    Narrower than `test_contributions`' fixture on purpose. ADR 0035 runs the
    carveout under `vault:write`, and a fixture that also held `vault:update`
    would pass every test here even if the route had quietly been wired to
    require replacement authority.
    """

    credential_id, token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))
    try:
        yield token
    finally:
        _drop(credential_id)

SUMMARY = "Establishes that the carveout fills an absent summary and nothing else."


def _summary_url(note_id: str) -> str:
    return f"/api/v1/vault/notes/{note_id}/summary"


def _row(note_id: str) -> dict:
    """The stored document and its embedding hash, as one mapping."""

    service, _engine = vault_service()

    async def read() -> dict:
        async with service.transaction() as connection:
            document = (
                await connection.execute(
                    select(
                        vault_documents.c.title,
                        vault_documents.c.body,
                        vault_documents.c.summary,
                        vault_documents.c.tags,
                        vault_documents.c.aliases,
                        vault_documents.c.content_revision,
                    ).where(vault_documents.c.id == note_id)
                )
            ).mappings().one()
            digest = (
                await connection.execute(
                    select(vault_document_embeddings.c.embedded_text_sha256).where(
                        vault_document_embeddings.c.document_id == note_id,
                        vault_document_embeddings.c.profile_id == PROFILE_ID,
                    )
                )
            ).scalar_one()
        return {**dict(document), "embedded_text_sha256": digest}

    return asyncio.run(read())


def _backdate(note_id: str, *, seconds: int) -> None:
    """Age a note past the grace period without waiting for it."""

    service, _engine = vault_service()

    async def age() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                update(vault_documents)
                .where(vault_documents.c.id == note_id)
                .values(created_at=datetime.now(UTC) - timedelta(seconds=seconds))
            )

    asyncio.run(age())


def test_a_contribution_without_a_summary_is_told_how_to_supply_one(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        response = client.post(
            "/api/v1/vault/contributions", json=_payload(), headers=headers
        )

        assert response.status_code == 200, response.text
        advice = response.json()["summary_advice"]
        assert advice is not None
        # Actionable, which means naming the operation and the deadline rather
        # than observing that something is missing. It does not repeat the note
        # id: `note_id` is already a field here, and this string is paid for
        # twice on the MCP wire (`test_mcp_budget`).
        assert "/summary" in advice
        assert str(SUMMARY_GRACE_PERIOD_SECONDS // 60) in advice
        assert response.json()["note_id"] not in advice
    finally:
        _cleanup()


def test_a_contribution_carrying_a_summary_draws_no_advice(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        response = client.post(
            "/api/v1/vault/contributions",
            json=_payload(summary=SUMMARY),
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["summary_advice"] is None
    finally:
        _cleanup()


def test_the_carveout_writes_the_summary_and_re_embeds_the_note(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        before = _row(note_id)
        calls_after_contribute = provider.calls

        response = client.post(
            _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["note_id"] == note_id

        after = _row(note_id)
        assert before["summary"] is None
        assert after["summary"] == SUMMARY
        # Caller-supplied content moved, so an amendment composed against the
        # old revision must now be stale (ADR 0028).
        assert after["content_revision"] == before["content_revision"] + 1
        assert response.json()["content_revision"] == after["content_revision"]

        # The embedding text changed, so exactly one further call was bought
        # and the stored hash describes the text that was actually embedded.
        # Nothing in the codebase repairs that hash later, so this is the
        # assertion that matters most in this module.
        assert provider.calls == calls_after_contribute + 1
        assert after["embedded_text_sha256"] != before["embedded_text_sha256"]

        class _Embeddable:
            title = after["title"]
            body = after["body"]
            summary = after["summary"]
            tags = tuple(after["tags"] or ())
            aliases = tuple(after["aliases"] or ())

        assert after["embedded_text_sha256"] == embedding_text_digest(
            assemble_embedding_text(_Embeddable())
        )
    finally:
        _cleanup()


def test_only_the_summary_is_reachable_through_this_route(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """One field, enforced by the model rather than by ignoring extras.

    A silently-dropped `body` would be worse than a refusal: the caller would
    believe it had rewritten the note.
    """

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)

        response = client.post(
            _summary_url(note_id),
            json={"summary": SUMMARY, "body": "an attempt at the body"},
            headers=headers,
        )

        assert response.status_code == 422
        assert _row(note_id)["summary"] is None
    finally:
        _cleanup()


def test_a_note_that_already_has_a_summary_is_refused(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Monotonic: the carveout completes a note, it never rewrites one."""

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token, summary=SUMMARY)

        response = client.post(
            _summary_url(note_id),
            json={"summary": "A different account of the same note."},
            headers=headers,
        )

        assert response.status_code == 409
        assert _row(note_id)["summary"] == SUMMARY
    finally:
        _cleanup()


def test_another_principal_cannot_describe_your_note(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """404 rather than 403, so authorship cannot be enumerated."""

    credential_id, other_token = _issue(scopes=(VaultScope.READ, VaultScope.WRITE))

    try:
        note_id = _contribute(client, write_token)

        response = client.post(
            _summary_url(note_id),
            json={"summary": SUMMARY},
            headers={"Authorization": f"Bearer {other_token}"},
        )

        assert response.status_code == 404
        assert _row(note_id)["summary"] is None
    finally:
        _cleanup()
        _drop(credential_id)


def test_the_grace_period_closes(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        _backdate(note_id, seconds=SUMMARY_GRACE_PERIOD_SECONDS + 60)

        response = client.post(
            _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
        )

        assert response.status_code == 409
        # The caller is told the rule, not merely refused: it has proved it
        # wrote this note, so it is owed the reason.
        assert response.json()["detail"]["grace_seconds"] == SUMMARY_GRACE_PERIOD_SECONDS
        assert _row(note_id)["summary"] is None
    finally:
        _cleanup()


def test_a_note_just_inside_the_window_is_still_accepted(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The boundary belongs to the caller, not to the clock's rounding."""

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)
        _backdate(note_id, seconds=SUMMARY_GRACE_PERIOD_SECONDS - 60)

        response = client.post(
            _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
        )

        assert response.status_code == 200, response.text
    finally:
        _cleanup()


def test_the_carveout_does_not_require_update_authority(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The boundary ADR 0035 actually draws.

    `vault:update` is off the OAuth baseline by design, so a carveout that
    needed it would be unreachable by exactly the credentials it exists for.
    The `write_token` fixture holds contribute and nothing else, so this
    passing is the whole point; it fails the moment the route asks for more.
    """

    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)

        response = client.post(
            _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
        )

        assert response.status_code == 200, response.text
        assert _row(note_id)["summary"] == SUMMARY
    finally:
        _cleanup()


def test_an_unknown_note_is_a_404(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    response = client.post(
        _summary_url(f"missing-{uuid4().hex[:8]}"),
        json={"summary": SUMMARY},
        headers={"Authorization": f"Bearer {write_token}"},
    )

    assert response.status_code == 404


def test_an_empty_summary_is_rejected_at_the_boundary(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    headers = {"Authorization": f"Bearer {write_token}"}

    try:
        note_id = _contribute(client, write_token)

        response = client.post(
            _summary_url(note_id), json={"summary": "   "}, headers=headers
        )

        assert response.status_code == 422
        assert _row(note_id)["summary"] is None
    finally:
        _cleanup()


_MCP_ENDPOINT = "/api/v1/vault/mcp/"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _mcp_call(client: TestClient, token: str, name: str, arguments: dict) -> dict:
    """One `tools/call` over the MCP transport, parsed out of its SSE frame."""

    response = client.post(
        _MCP_ENDPOINT,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    match = re.search(r"^data: (.*)$", response.text, re.MULTILINE)
    return json.loads(match.group(1) if match else response.text)


def test_the_round_trip_works_over_mcp_too(
    client: TestClient, write_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contribute, read the advice, act on it -- through the agent's own surface.

    The HTTP tests above cover the service. This covers the adapter an agent
    actually speaks, which resolves its provider through its own module
    namespace and renders its own errors; a carveout that worked only over
    HTTP would be one no agent could reach.
    """

    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.mcp.get_embedding_provider", lambda: stub)

    try:
        contributed = _mcp_call(
            client,
            write_token,
            "vault_contribute",
            {
                "title": f"An MCP note without a summary {uuid4().hex[:8]}",
                "body": "A body distinctive enough not to collide with anything.",
                "tags": ["testing"],
            },
        )
        outcome = json.loads(contributed["result"]["content"][0]["text"])

        assert outcome["status"] == "inserted"
        advice = outcome["summary_advice"]
        assert advice is not None
        assert "vault_set_summary" in advice

        settled = _mcp_call(
            client,
            write_token,
            "vault_set_summary",
            {"note_id": outcome["note_id"], "summary": SUMMARY},
        )

        assert settled["result"].get("isError") is not True, settled
        result = json.loads(settled["result"]["content"][0]["text"])
        assert result["note_id"] == outcome["note_id"]
        assert _row(outcome["note_id"])["summary"] == SUMMARY
    finally:
        _cleanup()


def test_content_changing_mid_flight_refuses_rather_than_indexing_stale_text(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The race that used to write a vector describing text the note no longer had.

    The embedding happens outside the corpus lock, against a snapshot read
    before it. An ordinary update taking the same lock can commit in between --
    so holding the lock says nothing about what happened before it was
    acquired. Writing anyway left `embedded_text_sha256` agreeing with the
    vector and disagreeing with the row, which nothing detects later, because
    the digest is a hash of the text that was embedded rather than of the note.

    Made deterministic by bumping `content_revision` in the database after the
    request has read its snapshot, which is exactly the interleaving.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    try:
        note_id = _contribute(client, write_token)
        before = _row(note_id)

        # The concurrent writer, landed in the one window that matters: after
        # the request read its snapshot and before it takes the lock. The
        # embedding call is that window, which is why it is the hook.
        service, _engine = vault_service()
        original_embed = provider.embed

        async def embed_then_let_someone_else_win(texts, kind):
            await _bump_revision(service, note_id)
            return await original_embed(texts, kind)

        provider.embed = embed_then_let_someone_else_win
        try:
            response = client.post(
                _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
            )
        finally:
            provider.embed = original_embed

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["retryable"] is True

        after = _row(note_id)
        # Nothing was written: no summary, and the vector still describes the
        # text it was computed from.
        assert after["summary"] is None
        assert after["embedded_text_sha256"] == before["embedded_text_sha256"]
    finally:
        _cleanup()


async def _bump_revision(service, note_id: str) -> None:
    async with service.transaction() as connection:
        await connection.execute(
            update(vault_documents)
            .where(vault_documents.c.id == note_id)
            .values(
                body="A different body entirely, committed by someone else.",
                content_revision=vault_documents.c.content_revision + 1,
            )
        )


def test_a_blank_summary_is_the_same_as_no_summary(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """Otherwise the note is undescribed and unrepairable at the same time.

    A whitespace-only summary used to be stored verbatim: non-null, so it
    suppressed the advice, refused `vault_set_summary` (which requires the
    column to be null), and was skipped by the backfill for the same reason.
    Nothing could notice it and nothing could fix it.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    try:
        response = client.post(
            "/api/v1/vault/contributions",
            json=_payload(summary="   "),
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        # Told to supply one, exactly as an omitted summary would be.
        assert body["summary_advice"] is not None

        stored = _row(body["note_id"])
        assert stored["summary"] is None

        # And the repair route is open, which is the point.
        repair = client.post(
            _summary_url(body["note_id"]),
            json={"summary": SUMMARY},
            headers=headers,
        )
        assert repair.status_code == 200, repair.text
    finally:
        _cleanup()


def test_a_replayed_contribution_still_advertises_an_open_repair(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The lost-response retry, which is the only case that matters here.

    A caller whose first response never arrived sees only the replay. Silence
    on the replay meant it never learned the note has no summary or that the
    window is running -- justified by an assumption ("the caller already acted
    on the first response") that a retry is precisely the evidence against.
    """

    headers = {"Authorization": f"Bearer {write_token}"}
    try:
        payload = _payload()
        first = client.post(
            "/api/v1/vault/contributions", json=payload, headers=headers
        )
        assert first.status_code == 200, first.text
        assert first.json()["summary_advice"] is not None

        replay = client.post(
            "/api/v1/vault/contributions", json=payload, headers=headers
        )

        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["summary_advice"] is not None
    finally:
        _cleanup()


def test_a_replay_stops_advertising_the_repair_once_it_is_done(
    client: TestClient, write_token: str, provider: StubEmbeddingProvider
) -> None:
    """The other half: advice tracks whether the repair is still possible, so
    it has to stop once the summary is there."""

    headers = {"Authorization": f"Bearer {write_token}"}
    try:
        payload = _payload()
        first = client.post(
            "/api/v1/vault/contributions", json=payload, headers=headers
        )
        note_id = first.json()["note_id"]

        repaired = client.post(
            _summary_url(note_id), json={"summary": SUMMARY}, headers=headers
        )
        assert repaired.status_code == 200, repaired.text

        replay = client.post(
            "/api/v1/vault/contributions", json=payload, headers=headers
        )

        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["summary_advice"] is None
    finally:
        _cleanup()
