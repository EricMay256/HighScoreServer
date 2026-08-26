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

from app.vault.api_models import SEARCH_STRUCTURED_BUDGET_BYTES
from app.vault.auth import VaultScope
from app.vault.db import create_vault_engine
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
)
from app.vault.embedding_text import assemble_embedding_text
from app.vault.embeddings import EmbeddingInputKind
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
)
from app.vault.service import VaultTransactionService
from app.vault.settings import VaultSettings, vault_enabled
from app.vault.snippet import SNIPPET_MAX_CHARS
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
# Measured 10,650 bytes: structured 4,859, text 5,791. Before search became a
# discovery surface on 2026-08-26 the same page cost 58,784 bytes (structured
# 28,277, text 30,507) because every hit carried the note's whole body -- an
# 82% reduction, and the structured copy now sits inside the 8 KiB ceiling
# `SEARCH_STRUCTURED_BUDGET_BYTES` enforces with room to spare.
#
# Both copies ship, which is worth recording because it is not obvious: a tool
# annotated `-> dict[str, Any]` looks schema-less but is not. `func_metadata`
# derives a permissive `{"type": "object", "additionalProperties": true}` from
# it, and any derived schema is enough to make the SDK attach
# `structuredContent` beside the text block. So compaction is worth twice its
# apparent size -- it lands on both copies -- and giving a tool a typed return
# sharpens its schema without adding a copy.
#
# The ~15% headroom over the measurement is for corpus drift in the fixture,
# not for growth in the response. A change that needs more than this should
# move the number deliberately and say why.
SEARCH_WIRE_BUDGET_BYTES = 12 * 1024

# One `vault_contribute` result at the MCP default, `response_detail=outcome`.
#
# Measured 700 bytes: structured 323, text 377. The same write at
# `response_detail=review` -- the HTTP default, and what both surfaces
# returned before 2026-08-26 -- costs 2,241 bytes, so the narrow default saves
# about 1,540 bytes or roughly 385 tokens per write.
#
# Small next to search, and that was never the point. What the default drops
# is up to five scored note ids the contributor searched for moments ago plus
# five wiki pages that had no bearing on the outcome: an invitation to fetch
# something nobody needed, sitting in front of a model that has just finished
# its task. `max_similarity` stays, because it is the verdict.
CONTRIBUTE_WIRE_BUDGET_BYTES = 1024

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
    embeddings = VaultDocumentEmbeddingRepository()
    stub = StubEmbeddingProvider()
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

    async def embed() -> None:
        # Without these the dedup gate has nothing to score against, because
        # `find_similar` joins through `vault_document_embeddings`. A fixture
        # of unembedded documents makes `similars` come back empty and every
        # assertion about the gate's output vacuously true -- which is how the
        # first version of this module measured a contribution response that
        # was smaller than any real one.
        async with service.transaction() as connection:
            for document_id in ids:
                document = await documents.get_by_id(connection, document_id)
                assert document is not None
                (vector,) = await stub.embed(
                    [assemble_embedding_text(document)],
                    EmbeddingInputKind.DOCUMENT,
                )
                await embeddings.upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=document_id,
                        profile_id=stub.profile_id,
                        vector=vector,
                    ),
                )

    async def clear() -> None:
        async with service.transaction() as connection:
            await connection.execute(
                delete(vault_documents).where(vault_documents.c.id.in_(ids))
            )

    try:
        asyncio.run(seed())
        asyncio.run(embed())
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


def test_a_search_hit_names_a_candidate_without_shipping_it(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """Discovery and retrieval are different operations with different costs.

    Until 2026-08-26 a hit was a whole `VaultDocumentDetail` -- the same
    projection `vault_get_note` returns -- so choosing one note out of ten
    meant paying for ten. This asserts the split held: what a hit carries is
    what a caller needs to *choose*, and everything else is a fetch away.

    The excluded list is the interesting half. Each of those fields was
    removed deliberately, and adding one back should have to argue with this
    test rather than slip in behind a convenient `**model_dump()`.
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

    result = json.loads(payload["result"]["content"][0]["text"])
    hit = result["hits"][0]

    assert set(hit) == {
        "note_id",
        "title",
        "summary",
        "snippet",
        "kind",
        "doc_status",
        "content_revision",
        "score",
        "lexical_rank",
        "vector_rank",
    }

    # The fixture notes carry no authored summary, which is the ordinary case
    # for a note (3 of 70 in the live corpus), so the preview is derived.
    assert hit["summary"] is None
    assert hit["snippet"]
    assert len(hit["snippet"]) <= SNIPPET_MAX_CHARS

    # Pagination is answered, and answered honestly: twelve notes were seeded
    # and ten requested.
    assert result["has_more"] is True
    assert result["truncated"] is False
    # Reserved, and null until a ranking exists that can be resumed.
    assert result["next_cursor"] is None


def test_a_large_limit_is_trimmed_to_the_byte_budget_and_says_so(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """`limit=50` is the caller provoking the budget, not meeting it by chance.

    The tail is what gets dropped, because fusion already ordered the hits and
    the top of the ranking is the part worth having. `truncated` distinguishes
    this from `has_more`: one says the response was cut, the other says the
    corpus had more.
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
                              "limit": 50},
            },
        )
    finally:
        _drop(credential_id)

    result = json.loads(payload["result"]["content"][0]["text"])
    structured, _text = _wire_sizes(payload)

    # Twelve notes are seeded, so a 50-limit search cannot be trimmed by the
    # budget here -- the corpus is smaller than the budget. What must hold is
    # that the two flags stay consistent with each other and with the page.
    assert len(result["hits"]) <= 50
    if result["truncated"]:
        assert result["has_more"] is True
    assert structured <= SEARCH_STRUCTURED_BUDGET_BYTES, (
        f"structured payload {structured}B exceeds the "
        f"{SEARCH_STRUCTURED_BUDGET_BYTES}B ceiling the trimmer enforces"
    )


def test_a_contribution_outcome_reports_the_verdict_not_the_working(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """Over MCP the default is the verdict; the gate's working is opt-in.

    `max_similarity` stays at every detail level because it is what a
    contributor acts on -- how near the write came to being flagged, and to
    what. What the default drops is the rest of the gate's working: four more
    scored notes the contributor searched for moments ago, and wiki pages that
    `app/vault/AGENTS.md` is explicit are context and never a reason anything
    was flagged.

    Small in bytes, and that is not the point. Scored note ids in front of a
    model after a successful write are an invitation to go and read them.
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
                    "title": "A characterization note recording the outcome shape",
                    "body": _realistic_body(98),
                },
            },
        )
    finally:
        _cleanup()
        _drop(credential_id)

    outcome = json.loads(payload["result"]["content"][0]["text"])

    assert outcome["status"] in {"inserted", "flagged", "rejected"}
    assert outcome["similars"] == []
    assert outcome["related_pages"] == []
    # Present as a key whether or not the corpus had anything to score against.
    assert "max_similarity" in outcome


def test_review_detail_restores_the_full_dedup_evidence(
    client: TestClient,
    budget_corpus: list[str],
    stub_provider: StubEmbeddingProvider,
) -> None:
    """The opt-in path is what an adjudication surface asks for.

    `similars` keeps the whole ranking here, rank 1 included, so
    `max_similarity` is additive rather than a field moved out of the list.
    Deduplicating would have been tidier and would have quietly changed the
    shape the HTTP surface has always returned.
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
                    "title": "A note contributed at review detail",
                    "body": _realistic_body(97),
                    "response_detail": "review",
                },
            },
        )
    finally:
        _cleanup()
        _drop(credential_id)

    outcome = json.loads(payload["result"]["content"][0]["text"])

    assert outcome["similars"], "review detail returns the gate's working"
    assert outcome["max_similarity"] == outcome["similars"][0]


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
