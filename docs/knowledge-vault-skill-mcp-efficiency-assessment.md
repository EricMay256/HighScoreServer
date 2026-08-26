# Knowledge Vault Skill and MCP Efficiency Assessment

**Status:** Implementation handoff  
**Date:** 2026-08-26  
**Audience:** Coding agent maintaining the Knowledge Vault skill and hosted MCP server

## Executive decision

The process assessment is directionally correct: the dominant cost came from violating progressive disclosure at the MCP result boundary. `vault_search` should return compact discovery metadata, and a separate fetch operation should return full note content only for selected candidates. The skill should also be reduced to a compact operational core with task-specific references.

Implement those two changes first. They address the observed failure directly and align with the current Agent Skills standard, OpenAI guidance, Anthropic guidance, and OpenAI's recommended `search`/`fetch` MCP pattern.

Several recommendations need refinement:

- Do **not** remove the text copy of `structuredContent` unconditionally. The current MCP specification and OpenAI integration guidance recommend returning both for backward compatibility. Fix duplication in capable clients, or keep the compatibility text compact where the integration permits it.
- Prefer separate `search` and `fetch` tools over allowing `vault_search(detail="full")`. A full-detail search makes the expensive path easy to invoke accidentally and weakens the clean discovery/retrieval boundary.
- Do **not** make unified-diff hunk counts optional or silently repair malformed diffs. Keep strict unified-diff validation. Add a structured exact-span editing operation for agents that should not need to author patch syntax.
- A batched multi-query search is useful, but it is lower priority than compact search results, cursor pagination, and explicit truncation metadata. It should merge rankings deterministically and expose which queries matched each result.

Expected impact: the demonstrated workflow should fall from tens of thousands of tool-result tokens to low thousands, with fewer opportunities for truncation-driven retries.

## Evidence from current guidance

### Generic Agent Skills

The Agent Skills specification explicitly uses three-stage progressive disclosure: metadata at discovery, the complete `SKILL.md` after activation, and referenced resources only when needed. It recommends keeping `SKILL.md` below 5,000 tokens and 500 lines, keeping reference files focused, and avoiding deeply nested reference chains. This directly supports moving setup, migration, compilation, and authentication material out of the operational core. [Agent Skills specification](https://agentskills.io/specification)

### ChatGPT and Codex

OpenAI documents the same progressive-disclosure model and recommends focused skills, imperative steps, explicit inputs and outputs, and trigger testing. ChatGPT/Codex load the entire selected `SKILL.md`, so material that is irrelevant to most invocations still has a real per-invocation cost. OpenAI also supports declaring MCP dependencies and invocation policy in `agents/openai.yaml`. [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)

For MCP servers, OpenAI recommends focused tools, explicit input and output schemas, accurate safety annotations, concise `structuredContent`, stable identifiers, and server-level instructions for cross-tool sequencing. It recommends putting the most important server instructions in the first 512 characters. [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)

For retrieval integrations, OpenAI's current compatibility pattern is explicitly two tools: metadata-oriented `search`, followed by full-content `fetch`. It also says to return structured results in both `structuredContent` and JSON-encoded text for compatibility. [OpenAI: MCP servers for plugins and API integrations](https://developers.openai.com/api/docs/mcp)

### Claude and Claude Code

Claude Code also follows the Agent Skills standard, loads skill bodies only when invoked, and supports references and scripts. Claude-specific frontmatter and dynamic-context features are not fully portable to claude.ai or other Agent Skills clients, so the core skill should remain standard-compatible and isolate host-specific enhancements. [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)

Anthropic's tool guidance says responses should contain only high-signal fields needed for the next decision, while tool descriptions should explain use, non-use, parameter semantics, limitations, and response behavior. It also recommends schema-valid input examples for format-sensitive tools. [Anthropic: Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)

Claude Code's MCP tool search defers full tool schemas, but server instructions and tool names still guide discovery. Claude truncates tool descriptions and server instructions at 2 KB, so critical routing guidance must come first. [Anthropic: Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)

Anthropic's broader context-engineering guidance reinforces the underlying principle: command output, file reads, and tool results all consume context, and performance degrades as context fills. Its MCP code-execution guidance shows that filtering intermediate tool data outside model context can reduce token use dramatically. [Anthropic: Claude Code best practices](https://code.claude.com/docs/en/best-practices), [Anthropic: Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)

### MCP protocol constraint

The 2026-07-28 MCP specification permits `structuredContent` with an `outputSchema`, requires conformance when a schema is declared, and says a server returning structured content should also return serialized JSON in a text block for backward compatibility. Therefore, duplicate wire representations are currently intentional protocol compatibility behavior, even though a capable host should avoid injecting both copies into model context. [MCP tool specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

## Assessment of the stated process analysis

### Agree

1. **The initial search payload was the dominant waste.** Returning complete bodies for every hit turns discovery into bulk retrieval and defeats the purpose of `vault_get_note`.
2. **Raw `CallToolResult` printing was wasteful.** When a client exposes `structuredContent`, orchestration code should extract only the fields needed for the next decision.
3. **Truncation should not trigger repeated semantic work.** A repeated search merely to reshape output is a client/orchestration defect. Filter the already-returned structured object inside the execution environment.
4. **A wiki page should not be fetched merely because it ranks highly.** Candidate selection should use `kind`, title, and summary. Prefer note-sized sources when they answer the question; fetch a wiki only when its synthesis is directly needed.
5. **Eight searches were excessive.** One primary conceptual query plus, only when necessary, one exact-term/alias query is a better default. A third query is justified only by degraded vector retrieval, ambiguity, or a specific uncovered axis.
6. **The skill contains too much rarely used operational material.** Setup, migration, compilation, and provider-specific registration belong in referenced documents.
7. **The malformed diff was preventable.** Format-sensitive operations need either deterministic client-side validation or a less syntax-fragile tool.
8. **Concise notes are preferable.** A durable note should preserve the insight, mechanism, evidence, and action without retaining session narrative.

### Push back or qualify

1. **“Never print raw MCP results” is a client rule, not a universal skill rule.** A skill can instruct an agent to prefer `structuredContent`, but the server and host determine what is injected. Put this requirement in both the skill and client/harness tests.
2. **Do not hard-cap fetches at two in all circumstances.** Use “normally one or two; exceed only with an explicit reason.” Amendment work, conflict resolution, or synthesis across independent sources can require more.
3. **Do not prescribe one to three searches as a rigid ceiling.** Use one primary query, one lexical fallback when needed, and additional searches only when the result state or task demands them. The key control is evidence-based continuation, not the number itself.
4. **Do not add `detail="full"` to search as the primary design.** Keep search metadata-only. If snippets are useful, add bounded previews; full content belongs to fetch.
5. **Do not remove duplicate `content` at the server by default.** That conflicts with current MCP backward-compatibility guidance. Prefer host-side selection of `structuredContent`. If the server knows a client profile supports structured-only results, negotiate that explicitly rather than changing the universal default.
6. **Do not make unified-diff counts optional.** Hunk counts are part of the unified-diff grammar and help detect corruption. Silently deriving or correcting them can make a malformed request appear to mean something the author did not intend.
7. **Do not over-consolidate tools solely for Claude.** Anthropic's API guidance favors fewer related tools, while OpenAI favors one focused tool per distinct action. For this server, `search`, `get`, `contribute`, and `propose amendment` are meaningfully distinct actions. Preserve those boundaries and use clear namespacing/descriptions.

## Additions missing from the proposal

### 1. Declare output schemas for every structured tool

Add exact JSON Schemas for search, get, contribute, and amendment responses. Validate server results against them in tests. This improves parsing, follow-up calls, and cross-client portability.

### 2. Add explicit truncation and pagination semantics

Search must never silently truncate. Return:

```json
{
  "query": "...",
  "vector_status": "used",
  "hits": [],
  "next_cursor": null,
  "has_more": false
}
```

Use opaque cursors rather than offsets if corpus mutation can reorder results. If a response is cut by a server-side byte/token budget, return `truncated: true` and a concrete instruction for narrowing or continuing.

### 3. Add bounded previews, not bodies

Metadata search can include an optional server-generated `snippet` with a strict character ceiling and matching highlights. Keep `summary` as authored metadata. A snippet answers “why did this match?” without turning search into fetch.

### 4. Define stable ranking semantics

Document that `score` is for ordering within one response and must not be treated as a stable cross-query threshold unless the algorithm guarantees that. For multi-query search, expose per-query rank/match provenance and a deterministic fused rank.

### 5. Add tool annotations and least-privilege surfaces

Mark search/get as read-only and non-destructive. Mark contribute/amendment accurately. Continue hiding update, delete, review, and compile tools from principals lacking those scopes; authorization checks must also remain server-side.

### 6. Use concise server instructions

Put the cross-tool invariant first:

> Search before contribute or amendment. Inspect `vector_status`; fetch only selected IDs. A flagged contribution is a settled outcome and must not be retried.

Keep server instructions below the strictest known client truncation limit and do not repeat parameter documentation already present in tool schemas/descriptions.

### 7. Add idempotency and retry contracts to schemas and tests

Keep content-derived idempotency for contributions. Return `idempotent_replay` explicitly. Define retry behavior for transport errors, `503`, validation failures, and settled duplicate outcomes.

### 8. Add observability for token-cost regressions

Record response byte size, hit count, body bytes accidentally included, result detail mode, latency, and client identity/version where available. Add a regression budget rather than relying on anecdotal traces.

### 9. Add cross-client contract tests

Test at least:

- ChatGPT/Codex client with `structuredContent` available.
- Claude Code with tool search enabled.
- A compatibility client that reads only text `content`.
- Read-only and read/write credentials.
- `vector_status` values `used`, `not_configured`, and `failed`.

### 10. Add skill trigger and workflow evals

Forward-test realistic prompts for:

- retrieve-only question;
- retrieve then contribute;
- likely duplicate;
- amendment to an existing note;
- malformed patch;
- service unavailable;
- Stage A setup/migration question that should load a reference;
- unrelated file-edit task that should not trigger the skill.

Measure tool calls, fetched documents, tool-result bytes/tokens, contribution outcome, and whether the agent obeyed `vector_status`.

## Recommended skill redesign

### Proposed directory

```text
knowledge-vault/
├── SKILL.md
├── references/
│   ├── stage-a.md
│   ├── service-setup-auth.md
│   ├── migration.md
│   ├── compilation.md
│   └── amendment-formats.md
└── scripts/
    └── validate_body_diff.py
```

Keep references one level from `SKILL.md`. Do not duplicate content between the core and references.

### Required `SKILL.md` core

The core should retain:

1. Scope: durable reusable insights, not transient notes or ordinary file edits.
2. Mode detection and the non-negotiable service rule: never write projected Agent Markdown directly.
3. Retrieve-first workflow.
4. `vector_status` branching.
5. Candidate selection: inspect compact metadata; fetch normally one or two directly relevant notes.
6. Contribution/amendment editorial bar.
7. Outcome handling, especially that `flagged` is settled and must not be retried.
8. Compact-result handling: prefer `structuredContent`; never echo raw envelopes; filter results in code rather than repeating calls.
9. Conditional links to references for setup, migration, compilation, and amendment syntax.

The service-vs-Stage-A writer rule is safety-critical and must remain in the core even if setup details move out.

### Suggested operational wording

```markdown
## Hosted-service workflow

1. Search once with a focused conceptual query.
2. Inspect `vector_status` and compact hit metadata. If lexical-only, make one exact-term or alias query. If failed, report degraded retrieval and do not claim absence of prior art.
3. Fetch normally one or two directly relevant notes. Prefer a note over a wiki page unless the wiki summary is directly on point.
4. Contribute one concise, self-contained insight or propose an amendment. Verify every related/source ID before sending it.
5. Interpret the returned status. Never retry `flagged` or `rejected` as though it were a transport failure.
6. Report only the outcome, note/proposal ID, and any actionable error.

When `structuredContent` is available, use it and extract only fields needed for the next step. Do not print or echo the raw result envelope. Do not repeat a completed search merely to make its output smaller; filter it locally.
```

## Recommended MCP redesign

### Priority 0: fix `vault_search`

Make search body-free by contract.

```json
{
  "query": "string",
  "vector_status": "used | not_configured | failed",
  "profile_id": "string | null",
  "hits": [
    {
      "note_id": "32-char-id",
      "title": "string",
      "summary": "string | null",
      "snippet": "bounded string | null",
      "kind": "note | wiki",
      "doc_status": "string",
      "score": 0.0,
      "content_revision": 1
    }
  ],
  "next_cursor": "opaque string | null",
  "has_more": false
}
```

Never include `body`, `tags`, `aliases`, relationships, or timestamps unless a demonstrated selection need justifies them. Tags may be useful as an opt-in metadata field, but do not return them by default.

### Priority 0: preserve `vault_get_note` as fetch

`vault_get_note` remains the full-record operation. Consider aliasing or renaming it to `vault_fetch_note` only if backward compatibility permits; the search/fetch pairing is more legible to generic clients. Return the complete record once, with an output schema.

### Priority 0: compact contribution outcomes

Default contribution response:

```json
{
  "status": "inserted | flagged | rejected | invalid",
  "note_id": "string | null",
  "idempotent_replay": false,
  "max_similarity": {
    "note_id": "string",
    "title": "string",
    "score": 0.0
  },
  "errors": []
}
```

Do not return five similar notes and five related pages by default. If useful for an interactive review surface, expose `response_detail="review"`; keep `response_detail="outcome"` as the default.

### Priority 1: structured body edit

Retain strict `vault_propose_note_body_diff`. Add an alternative:

```json
{
  "note_id": "...",
  "base_revision": 3,
  "expected_text": "exact old span",
  "replacement_text": "new span",
  "occurrence": 1,
  "rationale": "..."
}
```

Server behavior:

1. Reject if `base_revision` is stale.
2. Reject if `expected_text` does not occur exactly once unless `occurrence` disambiguates it.
3. Materialize a canonical unified diff for storage and review.
4. Return proposal ID, canonical diff, and status.

This removes manual hunk arithmetic while retaining explicit intent and reviewability. Do not silently reinterpret malformed unified diffs.

### Priority 2: multi-query search

Add only after the compact single-query path is measured. Suggested input:

```json
{
  "queries": ["conceptual phrasing", "exact terms and aliases"],
  "limit": 10
}
```

Requirements:

- maximum three queries;
- deduplicate by note ID;
- deterministic fusion;
- expose matched query indexes and per-query ranks;
- one shared `vector_status` only if all query arms share the same status, otherwise return status per query;
- enforce the same total response budget as single search.

## Scaling design: embeddings, chunking, and offline export

The vault is currently small, but its persistent formats should be chosen so corpus growth does not force an authority or identity migration. Optimize the schema for scale now; defer expensive indexes and universal chunk generation until measurements justify them.

### Current-state assessment

The available evidence indicates that the hosted service currently creates one embedding per note or wiki page rather than independently addressable chunks:

- search returns one hit per `note_id`, with no chunk ID, heading path, or text offsets;
- design notes describe a singular pgvector embedding column and pairwise note similarity;
- semantic deduplication is calibrated between complete notes;
- long wiki pages appear as single search results.

This is strong behavioral and design evidence, not direct database-schema inspection. An implementation could theoretically search hidden chunks and collapse them to notes, but no exposed contract or design note currently indicates that behavior. Confirm the schema and embedding assembler in code before migration work.

### Decision: add selective hierarchical chunking, not universal replacement

Retain one document-level embedding for every note. Add chunk embeddings only for long or semantically heterogeneous documents, initially for retrieval only.

Use document embeddings for:

- contribution deduplication;
- broad semantic discovery;
- short atomic Agent Notes;
- document-level relatedness;
- fallback when chunk generation is unavailable.

Use chunk embeddings for:

- locating narrow passages inside long wiki pages;
- producing bounded search snippets;
- section-level retrieval and citation;
- improving recall when unrelated sections dilute a whole-document embedding.

Do not replace note-level deduplication with maximum chunk similarity. More chunks create more chances for an accidental high similarity, and a short contribution matching one passage of a larger synthesis is not necessarily a duplicate of the whole document. Chunk and document similarity distributions require separate calibration.

### Counterarguments and mitigations

| Counterargument | Consequence | Mitigation |
|---|---|---|
| Atomic Agent Notes are already semantic chunks | Further splitting separates evidence, mechanism, and action | Do not chunk short notes; treat oversized atomic notes as editorial-review candidates |
| Long documents can dominate results with many matching chunks | Search diversity falls even as raw recall rises | Group by `note_id`; return one note result with its best chunk and optionally one diverse secondary passage |
| Chunks lose title and surrounding context | A section embedding may misrepresent qualifications or terminology | Embed a small context envelope: document title, heading ancestry, optional summary, then chunk text |
| Fixed windows break Markdown structures | Code blocks, tables, lists, and explanation/example pairs become incoherent | Use Markdown-aware section boundaries; merge small sections and split oversized sections by paragraph |
| Chunk boundaries change after edits | Re-embedding and identifiers churn | Store `chunker_version`, heading path, content hash, and stable logical locator separately |
| More vectors increase operational complexity | Storage, migrations, ranking, citations, and invalidation become harder | Introduce chunks behind a versioned table/export format and retain the current document path as fallback |
| Chunking can hide poor note design | Long multi-insight notes persist instead of being curated | Apply chunking freely to synthesized wiki pages but flag long Agent Notes for possible editorial splitting |

### Initial chunk eligibility policy

Treat thresholds as experiment inputs, not permanent constants:

- below roughly 800–1,200 tokens: normally document embedding only;
- above roughly 2,000 tokens: evaluate for chunking;
- between those ranges: chunk only when multiple substantial Markdown sections represent distinct concepts;
- preserve fenced code blocks and tables intact;
- merge undersized adjacent sections;
- split oversized sections at paragraph boundaries;
- start without overlap or with minimal overlap, then measure whether it improves recall enough to justify repeated content.

The project should first inspect the actual note-length and heading distributions and choose thresholds from that corpus.

### Proposed chunk schema

```text
note_chunks
├── chunk_id
├── note_id
├── content_revision
├── chunker_version
├── ordinal
├── heading_path
├── start_offset
├── end_offset
├── content_hash
├── embedding_profile
└── embedding
```

Do not identify a chunk only as `note-id:chunk-7`; inserting an early section would renumber every later chunk. Use a stable note ID plus normalized heading path and a content hash. Keep the human-readable logical locator separate from the hash that decides embedding reuse.

### Retrieval architecture with chunks

Run three retrieval arms:

1. lexical full-text search;
2. document-vector search;
3. chunk-vector search collapsed to note IDs.

Fuse note rankings rather than raw chunk rankings. Initially use rank-based fusion because lexical scores, document cosine, and chunk cosine are not directly comparable. Return a note-level hit containing only the best bounded snippet and a chunk locator; full content still requires `vault_get_note` or a future bounded section-fetch tool.

A bounded section fetch may eventually be useful for large wiki pages:

```json
{
  "note_id": "...",
  "chunk_id": "...",
  "include_neighbors": 1
}
```

It should supplement, not overload, metadata search.

### Offline vector export: authority boundaries

Use the exported Markdown tree as the authoritative offline content projection. Treat exported embeddings as a reusable derived artifact and the machine-local ANN/FTS database as a disposable cache:

```text
Hosted database
    -> Markdown export (authoritative offline content)
    -> portable vector export (reusable derived computation)
    -> local SQLite/FTS/HNSW index (disposable implementation cache)
```

Every exported vector must carry or reference:

```json
{
  "note_id": "...",
  "chunk_id": null,
  "content_revision": 1,
  "content_hash": "sha256:...",
  "model": "provider/model-name",
  "dimensions": 1536,
  "encoding": "float16",
  "normalization": "unit-length",
  "chunker_version": null,
  "embedded_at": "ISO-8601 timestamp"
}
```

An offline reader must ignore a vector whose content hash, embedding profile, dimensions, or chunker version does not match the exported content and local query model.

### Storage estimates

Raw vector storage is:

```text
vector_count * dimensions * bytes_per_component
```

For 1,536 dimensions:

| Encoding | Bytes per vector | 10,000 vectors | 100,000 vectors |
|---|---:|---:|---:|
| Float32 | 6 KiB | about 59 MiB | about 586 MiB |
| Float16 | 3 KiB | about 29 MiB | about 293 MiB |
| Int8 | 1.5 KiB | about 15 MiB | about 146 MiB |

Add metadata plus ANN-index overhead, commonly tens of percent and sometimes approaching the raw-vector size depending on implementation and HNSW parameters. Chunk count is the dominant multiplier: 10,000 notes averaging three vectors each require about 88 MiB of raw Float16 vectors before index overhead.

Prefer Float16 for portable export unless evaluation demonstrates unacceptable recall loss. Avoid decimal JSON arrays for vector values; textual encoding can multiply storage and parsing cost several-fold.

### One sidecar per note versus a consolidated store

#### Per-note sidecars

Example: `embeddings/<note-id>.f16` plus a small header or manifest record.

Advantages:

- one note update changes one small vector artifact;
- Git and file synchronization transfer only changed notes/vectors;
- deletion, invalidation, corruption, and subset copying have a small scope;
- content-addressed reuse is straightforward;
- raw vectors are accessible without a database runtime.

Disadvantages:

- thousands of small files increase inode, directory-scan, Git-object, and synchronization overhead;
- Android and Windows scanning can become material as the corpus grows;
- filesystem allocation can waste space for 1.5–3 KiB vectors;
- nearest-neighbor search still requires loading or compiling the files into an index;
- Markdown and sidecar updates are not atomic;
- model migrations touch every sidecar and repeated headers waste space.

#### Consolidated vector store

Examples: SQLite, Arrow/Parquet, or a binary vector pack plus an offset manifest.

Advantages:

- dense storage and fast sequential or memory-mapped reads;
- low per-vector overhead;
- efficient batch calculations and straightforward local search initialization;
- centralized provenance and easier support for multiple embedding profiles;
- SQLite can make metadata/vector updates transactional.

Disadvantages:

- one logical update can mutate a large binary file;
- Git diffs and merges are ineffective;
- synchronizing a database while it is open risks inconsistent snapshots;
- corruption has a larger blast radius;
- partial-vault copies no longer automatically carry matching vectors;
- an ANN index embedded in the store may be library/version-specific rather than portable.

#### Decision factors

| Factor | Per-note sidecars | Consolidated store |
|---|---|---|
| Frequent isolated note edits | Strong | Moderate |
| Git review and incremental commits | Strong | Weak |
| Syncthing transfer granularity | Strong, but many scans | Depends on block reuse |
| Android filesystem performance | Weak at large counts | Strong |
| Ready-to-query offline search | Requires compilation | Strong |
| Partial-vault copying | Strong | Weak |
| Large vector counts | Weakening fit | Strong |
| Atomic updates | Weak across Markdown/vector pair | Strong inside database |
| Portable reusable embeddings | Strong if format is simple | Strong if ANN state is excluded |
| Reproducible disposable cache | Unnecessary file proliferation | Natural fit |

### Recommended scalable compromise

For this project, use:

1. Markdown for authoritative offline-readable content.
2. An append-only consolidated Float16 vector pack with a JSONL or SQLite manifest for reusable embeddings.
3. A separately generated local SQLite FTS/vector or HNSW index as disposable cache data.

An append-only pack avoids rewriting the entire vector corpus when one note changes: append the new vector and update the manifest pointer; compact obsolete records periodically. If synchronization of the pack becomes expensive, shard it deterministically by a stable ID/hash prefix. Do not commit or synchronize a live mutable SQLite ANN database as though it were portable source data.

Per-note sidecars remain a reasonable simpler first implementation while the corpus is small, especially if incremental Git/Syncthing behavior is the dominant requirement. If chosen, make the sidecars a replaceable export format behind a manifest contract so they can later be packed without changing note IDs, content hashes, or the local MCP interface.

### Scaling decision summary

- Design identifiers, provenance, content hashes, model profiles, and chunker versions now.
- Keep whole-note embeddings permanently, even after chunk retrieval ships.
- Generate chunks selectively and asynchronously.
- Keep deduplication note-level until a separate chunk-aware dedup experiment proves value.
- Collapse chunk hits to note-level results before returning them.
- Export embeddings independently of the ANN implementation.
- Treat local indexes as rebuildable caches.
- Begin with the simplest storage representation, but hide it behind a versioned manifest/export contract so growth does not require an authority migration.

### Chunking and storage evaluation gates

Before enabling chunk search by default, compare document-only and hierarchical retrieval using a labeled query set. Measure recall@5/10, reciprocal rank, note-result diversity, redundant chunk frequency, result bytes, fetch count, and latency. Include narrow wiki-section questions, broad whole-note questions, exact error codes, paraphrases, and short atomic-note queries.

Before choosing permanent vector packaging, measure export size, incremental update bytes, scan time on Windows and Android, local index build time, corruption recovery, and partial-vault behavior at projected corpus sizes such as 10,000, 100,000, and 1,000,000 vectors.

## Implementation sequence

1. Add output models/schemas and snapshot the current responses.
2. Change search serialization to omit bodies and nonessential fields.
3. Add cursor/truncation fields and bounded snippets.
4. Make contribution outcome compact by default.
5. Update server instructions and tool descriptions, putting critical routing first.
6. Refactor `SKILL.md`; move conditional material into the proposed references.
7. Add deterministic diff validation script and structured span-edit tool.
8. Confirm the current database schema and embedding assembler are document-level; record the finding in an ADR.
9. Add embedding provenance fields and a versioned portable-export manifest before adding chunks.
10. Measure note length/heading distributions and build the chunking evaluation corpus.
11. Implement selective chunk generation and note-level grouping behind a feature flag.
12. Prototype the append-only Float16 vector pack and disposable local index; benchmark against per-note sidecars on Windows and Android.
13. Add contract, cross-client, skill-trigger, retrieval-quality, and token-budget tests.
14. Measure the original example workflow before and after.
15. Consider batched multi-query search only if traces still show repeated-search overhead.

## Acceptance criteria

- `vault_search` never returns note bodies.
- Ten ordinary search hits fit within an agreed response budget; suggested initial ceiling: 8 KiB serialized structured data, excluding protocol framing.
- Search declares and satisfies an output schema.
- Search exposes `vector_status`, `has_more`, and `next_cursor`.
- A normal retrieve-and-contribute workflow uses one search, one or two fetches, and one write.
- No agent repeats a search solely to compact a previously returned result in eval runs.
- A capable client places only one logical copy of structured results into model context, while a text-only compatibility client still works.
- Contribution defaults to outcome-only detail.
- `flagged` and `rejected` do not trigger retries.
- Strict unified diffs remain strict; the structured edit path generates a canonical review diff.
- The core `SKILL.md` is materially shorter and setup/migration/compile references load only for matching tasks.
- ChatGPT/Codex and Claude Code trigger and execute the core workflow successfully in representative evals.
- Every embedding is traceable to a content hash, content revision, model/profile, dimensions, encoding, and chunker version where applicable.
- Short atomic notes retain only document embeddings unless evaluation justifies otherwise.
- Chunk retrieval is grouped to note-level hits and cannot flood a result page with passages from one document.
- Deduplication behavior and calibrated thresholds remain unchanged during the initial chunking rollout.
- The portable vector export can rebuild the local search index without contacting an embedding provider.
- Per-note and packed export prototypes are compared using measured Windows/Android scan time, incremental synchronization bytes, index build time, and projected vector counts.

## Final disposition of the original recommendations

| Recommendation | Disposition |
|---|---|
| Metadata-only search | Adopt immediately |
| Search detail modes (`summary` or `full`) | Replace with metadata-only search plus separate fetch; optional bounded snippet |
| Remove duplicate `content`/`structuredContent` | Do not adopt universally; preserve protocol compatibility and deduplicate client context |
| Batched deduplicating search | Adopt later if measurement justifies it |
| Outcome-only contribution response | Adopt immediately; make expanded review detail opt-in |
| Optional/canonicalized diff hunk counts | Reject; keep strict diffs |
| Structured exact-span edit | Adopt, with base revision and exact-match safeguards |
| Compact `SKILL.md` plus references | Adopt immediately |
| Never print raw MCP envelope | Adopt as agent/client behavior, backed by harness tests |
| One to three searches; one to two fetches | Adopt as a default heuristic, not an absolute ceiling |
| Keep notes concise | Adopt; preserve evidence, mechanism, and action |
| Universal note chunking | Reject; adopt selective hierarchical retrieval chunking while preserving whole-note embeddings |
| One vector sidecar per note | Acceptable small-corpus starting point, but hide behind a versioned manifest |
| Consolidated vector/index database | Split the concern: portable append-only vector pack plus disposable local search index |

## Sources

- [Agent Skills specification](https://agentskills.io/specification)
- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)
- [OpenAI: MCP servers for plugins and API integrations](https://developers.openai.com/api/docs/mcp)
- [OpenAI MCP plugin changelog](https://developers.openai.com/plugins/changelog)
- [MCP 2026-07-28 tool specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Anthropic: Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
- [Anthropic: Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)
- [Anthropic: Claude Code best practices](https://code.claude.com/docs/en/best-practices)
- [Anthropic: Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
