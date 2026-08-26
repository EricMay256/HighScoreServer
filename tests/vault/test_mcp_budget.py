"""What one MCP tool result costs a model, measured rather than estimated.

Every byte a tool returns is injected into the caller's context, so a tool
result has a size budget in the same way a request has a latency budget. This
module measures that size against a corpus of realistically-sized notes and
fails when it regresses, which is the only way a token-cost change shows up as
a red test rather than as a bill.

**Both copies are counted, and the count is the point.** The MCP specification
says a server returning `structuredContent` should also return the same data
serialized into a text block, so a client predating structured output still
works. The SDK honours that: `convert_result` builds the text block first and
attaches `structuredContent` alongside it whenever the tool declares an output
schema. A capable host may then place one copy in the model's context, or both
-- that is the host's decision, not the server's. The budget therefore counts
what the server *sends*, because that is the only figure the server controls.

**Every vault tool already ships both copies**, which is the measurement that
surprised this module into existence. A tool annotated `-> dict[str, Any]`
reads as schema-less and is not: `func_metadata` derives a permissive
`{"type": "object", "additionalProperties": true}` from that annotation, and
any derived schema is enough for the SDK to attach `structuredContent`. So the
doubling is not a future cost of typing these returns -- it is the current
state, and the only thing typed models buy is a schema worth validating
against. Compaction is the whole saving, and it is worth twice its apparent
size because it lands on both copies at once.

The numbers recorded in the budget constants below are the *measured* state of
this branch, not aspirations. Lowering one is the deliverable of a compaction
change; raising one needs a reason in the commit message.
"""

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.vault.auth import VaultScope
from app.vault.db import create_vault_engine
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.repository import VaultDocumentRepository
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings, vault_enabled
from app.vault.tables import vault_documents
from tests.vault.test_contributions import StubEmbeddingProvider, _cleanup
from tests.vault.test_mcp import _rpc
from tests.vault.test_routes import _drop, _issue


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="The vault MCP adapter is only mounted when VAULT_ENABLED is true",
)


# ------------------------------------------------------------- budgets ----

# One `vault_search` result, ten hits, counting both wire copies.
#
# Measured on this branch at 58,784 bytes: structured 28,277, text 30,507. A
# hit carries the note's complete body, so the cost is the corpus's mean note
# length times the page size, doubled by the compatibility text block. The
# corpus this serves (2026-08-26: 70 notes, mean body 2141 bytes, p90 4023) is
# what the fixture below is sized from.
#
# Both copies already ship, which is worth recording because it is not
# obvious: a tool annotated `-> dict[str, Any]` looks schema-less but is not.
# `func_metadata` derives a permissive `{"type": "object",
# "additionalProperties": true}` from it, and any derived schema is enough to
# make the SDK attach `structuredContent` beside the text block. Replacing
# those annotations with typed models therefore sharpens the schema without
# changing the number of copies -- it is close to free, not a doubling.
#
# The target, once search returns discovery metadata instead of documents, is
# 8 KiB of *structured* data per the efficiency assessment's acceptance
# criteria -- so roughly 17 KiB across both copies. This constant is what that
# work moves.
SEARCH_WIRE_BUDGET_BYTES = 60 * 1024

# One `vault_contribute` result. Small by comparison and included so the write
# path cannot quietly grow: the response carries no note body, only the dedup
# gate's evidence -- up to five similar notes and five related wiki pages, each
# a note id, a title and a score.
CONTRIBUTE_WIRE_BUDGET_BYTES = 4 * 1024

# The page size every budget above is quoted at. Ten is `vault_search`'s
# default limit, so it is the size a caller gets without asking for one.
BUDGET_PAGE_SIZE = 10


# -------------------------------------------------------------- corpus ----

# Sized from the live corpus rather than invented: a fixture of one-line
# bodies would report a comfortable budget for a response shape that is not
# comfortable in production, which is the failure mode this whole module
# exists to catch.
_MEAN_NOTE_BODY_BYTES = 2141

_BODY_TEMPLATE = """\
A rate-limit decorator on a {subject} cannot protect work done in that
{subject}'s dependencies, because dependencies are resolved before the handler
is called. The failure is silent and looks fixed.

Verified rather than reasoned, with a dependency that counts calls. A decorator
runs last. Anything a guard must precede has to run first, so the fix is a
router-level dependency that the framework solves before the per-route ones.

Ordering is the whole mechanism, so it is worth an explicit test: send more
requests than the limit allows and assert the inner dependency was reached only
`limit` times. Asserting the refusal alone passes under the broken arrangement
too, which is exactly why the bug survives review.

Generalizes past rate limiting. Any cross-cutting guard whose value is that it
runs early -- auth, quota, circuit breaker, request-size check -- is in the
wrong place if it is attached to the handler, in any framework where the
handler is the innermost layer. Ask what runs before it, not whether it runs.
"""


def _realistic_body(index: int) -> str:
    """A note body at the corpus's mean length, deterministic per index.

    Padded rather than truncated so the shared vocabulary at the top survives:
    the lexical arm has to match every seeded note for one query, or the page
    comes back short and the measurement is of the wrong thing.
    """

    body = _BODY_TEMPLATE.format(subject=f"handler-{index}")
    if len(body) >= _MEAN_NOTE_BODY_BYTES:
        return body
    filler = f"\nObserved in run {index} while auditing the guard ordering.\n"
    while len(body) < _MEAN_NOTE_BODY_BYTES:
        body += filler
    return body


def _vault_service() -> tuple[VaultTransactionService, Any]:
    settings = replace(VaultSettings.from_environment(), enabled=True)
    engine, observer = create_vault_engine(settings)
    return VaultTransactionService(engine, observer), engine


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> StubEmbeddingProvider:
    """Keep the measurement off the network.

    Not merely tidiness. Left unstubbed these tests reach the configured
    OpenAI endpoint -- observed doing exactly that on first run -- which makes
    a size assertion cost money, depend on a third party being up, and vary
    with whatever the provider returns. A budget is a property of the response
    shape, and the shape does not depend on which vector came back.

    Patched on `app.vault.mcp` rather than `app.vault.routes`: each adapter
    resolves the provider through its own module namespace, so patching the
    other one leaves this path live.
    """

    stub = StubEmbeddingProvider()
    monkeypatch.setattr("app.vault.mcp.get_embedding_provider", lambda: stub)
    return stub


@pytest.fixture
def budget_corpus(configure_test_env: None) -> Any:
    """Seed a full page of realistically-sized notes, then remove them.

    Twelve rather than ten so the page is genuinely full and `has_more` has
    something to be true about once search learns to report it.
    """

    service, engine = _vault_service()
    run_id = uuid4().hex
    documents = VaultDocumentRepository()
    ids: list[str] = []

    async def seed() -> None:
        async with service.transaction() as connection:
            for index in range(12):
                document_id = f"budget-{run_id}-{index:02d}"
                ids.append(document_id)
                await documents.insert(
                    connection,
                    NewVaultDocument(
                        id=document_id,
                        kind=DocumentKind.NOTE,
                        vault_path=f"Agent/notes/{document_id}.md",
                        status=DocumentStatus.ACTIVE,
                        title=(
                            f"A route decorator cannot guard work done in "
                            f"dependency {index:02d}"
                        ),
                        body=_realistic_body(index),
                        tags=("python", "gotcha", "tooling", "reference"),
                        contributed_by="test:budget-fixture",
                        provenance={"fixture": True},
                    ),
                )

    async def clear() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                delete(vault_documents).where(vault_documents.c.id.in_(ids))
            )

    try:
        asyncio.run(seed())
        yield ids
    finally:
        asyncio.run(clear())
        asyncio.run(engine.dispose())


# --------------------------------------------------------- measurement ----


def _wire_sizes(payload: Any) -> tuple[int, int]:
    """Bytes of each copy the transport carries for one tool result.

    Returns ``(structured_bytes, text_bytes)``. A tool that declares no output
    schema has no structured copy, and reports zero for it -- which is a real
    measurement of today's behaviour, not a missing one.

    The structured copy is re-serialized compactly rather than measured from
    the raw frame, because the two must be compared on the same terms: the
    text copy is already compact JSON, and scoring the structured copy with
    whatever whitespace the transport happened to use would compare formatting
    rather than content.
    """

    result = payload["result"]
    text = result["content"][0]["text"]
    structured = result.get("structuredContent")
    structured_bytes = (
        0
        if structured is None
        else len(
            json.dumps(
                structured, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
    )
    return structured_bytes, len(text.encode("utf-8"))


def _describe(label: str, structured: int, text: int, budget: int) -> str:
    total = structured + text
    return (
        f"{label}: structured={structured}B text={text}B total={total}B "
        f"budget={budget}B ({total / budget:.0%} of budget). "
        f"Lowering the budget is a deliverable; raising it needs a reason."
    )


# ------------------------------------------------------------- budgets ----


def test_a_full_search_page_stays_within_its_wire_budget(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """Ten hits, both copies, against the recorded ceiling."""

    credential_id, token = _issue((VaultScope.READ,))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "vault_search",
                "arguments": {"query": "decorator dependency guard ordering",
                              "limit": BUDGET_PAGE_SIZE},
            },
        )
    finally:
        _drop(credential_id)

    structured, text = _wire_sizes(payload)
    total = structured + text

    # A budget measured against a short page measures nothing. Assert the page
    # is full first, so a retrieval regression cannot masquerade as a saving.
    result = json.loads(payload["result"]["content"][0]["text"])
    assert len(result["hits"]) == BUDGET_PAGE_SIZE, (
        f"expected a full page to measure; got {len(result['hits'])} hits"
    )

    assert total <= SEARCH_WIRE_BUDGET_BYTES, _describe(
        "vault_search", structured, text, SEARCH_WIRE_BUDGET_BYTES
    )


def test_a_search_hit_currently_carries_the_whole_note(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """Characterization, not endorsement: this is the behaviour being changed.

    A search hit is today a complete `VaultDocumentDetail` -- the same
    projection `vault_get_note` returns -- plus three ranking fields. So the
    discovery call and the retrieval call return the same thing, and paying
    for ten documents to choose one is not a quirk of some query, it is the
    contract.

    Asserted rather than merely described so that making search a metadata
    surface has to come here and say so. A compaction change that left this
    passing would not have compacted anything.
    """

    credential_id, token = _issue((VaultScope.READ,))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "vault_search",
                "arguments": {"query": "decorator dependency guard ordering",
                              "limit": BUDGET_PAGE_SIZE},
            },
        )
    finally:
        _drop(credential_id)

    hit = json.loads(payload["result"]["content"][0]["text"])["hits"][0]

    # The fields a caller needs to *choose* a note.
    assert {"note_id", "title", "score"} <= set(hit)

    # The fields that make choosing cost as much as fetching. Each one is
    # removed by the metadata-only search change; this list is its checklist.
    assert hit["body"], "a hit carries the note's full body"
    for field in (
        "tags",
        "aliases",
        "facets",
        "related_ids",
        "source_ids",
        "vault_path",
        "doc_type",
        "status",
        "created_at",
        "updated_at",
    ):
        assert field in hit, f"a hit still carries {field}"

    # And the fields a compact hit will need but does not have yet.
    assert "snippet" not in hit
    assert "has_more" not in json.loads(
        payload["result"]["content"][0]["text"]
    )


def test_a_contribution_outcome_currently_carries_the_dedup_evidence(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """Characterization: `similars` and `related_pages` ship by default.

    Both are small -- a note id, a title and a score apiece -- so this is not
    where the tokens are. It is recorded because the fields invite a follow-up
    the caller does not need: the contributor already searched, and
    `related_pages` is documented in `app/vault/AGENTS.md` as context that is
    never the reason a contribution was flagged. The outcome-only default
    keeps the single closest note and drops the rest.
    """

    credential_id, token = _issue((VaultScope.READ, VaultScope.WRITE))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "vault_contribute",
                "arguments": {
                    "title": "A characterization note recording today's outcome shape",
                    "body": _realistic_body(98),
                },
            },
        )
    finally:
        # The write path really wrote. Sweeping by principal is what
        # test_contributions does and for the reason its comment gives: a
        # stray active document perturbs the dedup query and the lexical arm
        # for every later test. Leaving these behind broke seven search tests
        # before this call existed.
        _cleanup()
        _drop(credential_id)

    outcome = json.loads(payload["result"]["content"][0]["text"])

    assert outcome["status"] in {"inserted", "flagged", "rejected", "invalid"}
    assert "similars" in outcome
    assert "related_pages" in outcome
    assert "max_similarity" not in outcome
    assert "response_detail" not in outcome


def test_a_contribution_outcome_stays_within_its_wire_budget(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """One write, both copies, against the recorded ceiling.

    Seeded against the same corpus so the dedup gate has neighbours to report:
    an outcome measured against an empty vault would carry no `similars` and
    would flatter the shape under test.
    """

    credential_id, token = _issue((VaultScope.READ, VaultScope.WRITE))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "vault_contribute",
                "arguments": {
                    "title": "A budget fixture note that will not survive the test",
                    "body": _realistic_body(99),
                    "tags": ["python", "gotcha"],
                },
            },
        )
    finally:
        _cleanup()
        _drop(credential_id)

    structured, text = _wire_sizes(payload)
    total = structured + text

    assert total <= CONTRIBUTE_WIRE_BUDGET_BYTES, _describe(
        "vault_contribute", structured, text, CONTRIBUTE_WIRE_BUDGET_BYTES
    )
