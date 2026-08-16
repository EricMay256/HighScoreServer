# 5. OpenAI text-embedding-3-small as the first embedding profile

Date: 2026-07-28

## Status

Accepted

## Context

Vault ADR 0003 established that embeddings live in `vault_document_embeddings` keyed by
`(document_id, profile_id)`, and deliberately left `profile_id` unchosen: baking a guess into
revision 0001 would have encoded a decision nobody had made. The read-only retrieval slice
cannot proceed without one, because `profile_id` is written into every embedding row and
determines which vectors are comparable.

Migration 0001 pins the column at `vector(1536)` and indexes it with HNSW, which requires a
fixed width. **The ability to emit exactly 1536 dimensions is therefore a hard filter, not a
preference.** `vault-configuration.md` additionally required evaluating at least one managed
and one open-weight option against retrieval quality, query/document modes, latency, batch
limits, data-retention terms, cost, rate limits, and operational burden.

Verified against vendor documentation on 2026-07-28:

| Candidate | 1536 dimensions | Query/document modes | Cost per 1M tokens |
| --------- | --------------- | -------------------- | ------------------ |
| OpenAI `text-embedding-3-small` | Native | None (symmetric) | $0.02 ($0.01 batch) |
| OpenAI `text-embedding-3-large` | Via `dimensions` (from 3072) | None (symmetric) | $0.13 |
| Cohere `embed-v4.0` | Listed `output_dimension` | `search_query` / `search_document` | $0.12 |
| Google `gemini-embedding-001` | MRL truncation (from 3072) | `RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT` | ~$0.15 |
| Voyage `voyage-3.5-lite` | **No** — 2048/1024/512/256 only | Yes | $0.02 |
| Qwen3-Embedding-4B (Apache-2.0) | MRL, 32–2560 | Instruction prefix | Self-hosted |

Voyage was eliminated by the dimension filter despite competitive quality and price; adopting
it would require the dimension-change DDL that is still an open decision.

Qwen3-Embedding-4B was the strongest open-weight candidate and was rejected on operational
burden rather than quality. Serving it needs a GPU inference host, which is new infrastructure
that the host repository's standing guidance forbids introducing without explicit approval, and
which is disproportionate to a single-operator knowledge corpus running on an Essential-0 dyno.

## Decision

The first embedding profile is **`openai/text-embedding-3-small:1536`**.

The adapter calls the REST endpoint directly through `httpx.AsyncClient`, which HighScoreServer
already depends on, rather than the `openai` SDK. The request is one POST with a JSON body, so
the SDK would add a dependency and a second async transport for no gain. **This slice therefore
introduces no new package.**

`dimensions` is sent explicitly on every request rather than relying on the model default, so
the persisted `vector(1536)` contract is stated at the call site. This requires a v3 embedding
model; `text-embedding-ada-002` rejects the field.

The application-facing port (`embeddings.py`) carries an `EmbeddingInputKind` of `DOCUMENT` or
`QUERY` even though this provider ignores it. The distinction belongs to the port because
adopting an asymmetric provider must be a new adapter, not a signature change at every call
site.

## Consequences

The chosen model is the only shortlisted candidate whose *native* output is 1536, so no vector
is a truncation of a larger one. It is also the cheapest by a factor of six, and it needs no new
infrastructure, no new dependency, and no vendor account beyond an API key.

The accepted cost is that **`text-embedding-3-small` has no query/document asymmetry**. Cohere
`embed-v4.0` and `gemini-embedding-001` both do, and both would likely retrieve slightly better
for the same corpus. The judgement is that with a lexical arm running alongside the vector arm
and fusing through RRF (vault ADR 0006), the asymmetry gain does not justify six times the cost
and a second vendor relationship at this corpus size. If retrieval quality proves inadequate,
`cohere/embed-v4.0:1536` is the designated next candidate and — because ADR 0003 made profiles
additive — trying it is a backfill under a second `profile_id`, not a migration.

OpenAI retains API inputs for up to 30 days for abuse monitoring and does not train on API data
by default. Note bodies are sent to a third party on every re-embed, which is a property of any
managed provider and the reason the self-hostable option was evaluated rather than dismissed.

Changing provider or model remains a controlled re-embedding, never a credentials-only
configuration change. The procedure is in `vault-configuration.md`.

Rate limits are account-tier dependent (Tier 1 is on the order of thousands of requests and
1M tokens per minute for this model), and the array limit is 2048 inputs per request. The
adapter batches well below that ceiling because the per-request *token* cap binds first for
long documents.
