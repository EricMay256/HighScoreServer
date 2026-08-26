# 32. A contribution reports its verdict; the gate's working is opt-in

Date: 2026-08-26

## Status

Accepted 2026-08-26.

Refines ADR 0016 (the governed write path), which decided what a contribution *does* and left
the shape of what it *reports* to accrete. Sibling of ADR 0031, which made the same move for
search.

## Context

A settled write returned up to five `similars` — the notes the dedup gate scored the candidate
against — and up to five `related_pages`, the compiled wiki pages nearest the result. Ten
scored note ids, on every contribution, whether or not anything was near.

The bytes are not the problem. Measured 2026-08-26: 2,241 bytes across both wire copies at
full detail, against 700 at the narrow one. About 385 tokens.

The problem is what those ids invite. A model that has just finished writing a note is handed
a ranked list of other notes with scores beside them, and the reasonable next move looks like
reading some. That is a fetch nobody needed:

- **The contributor already searched.** Retrieve-first is the workflow the skill mandates and
  the server instructions repeat. The gate's candidates are not identical to what that search
  returned — the gate scores the *candidate note's own embedding*, not the user's question, so
  it is a different neighbourhood — but the contributor has already formed a view of the
  corpus around this topic.
- **`related_pages` is documented as never being the reason.** `app/vault/AGENTS.md` is
  explicit: a compiled page restates the notes it was built from, so resembling one is
  expected and is never why a contribution is flagged. On a write response it is scored ids
  with no bearing on the outcome.
- **Ranks 2 to 5 do not change any decision.** On `inserted` the gate passed and the runners-up
  are irrelevant. On `flagged` or `rejected` what matters is *what it collided with*, which is
  rank 1.

The two transports also have genuinely different callers. HTTP is a program, sometimes one
building an adjudication surface, and its contract already carried the full working. MCP is a
model with a context budget.

## Decision

**`max_similarity` is the verdict and is always returned. The rest of the gate's working is
returned only when asked for.**

`VaultContributionResponse` gains `max_similarity`: the single closest existing note, or null
when the corpus held nothing to score against. `response_detail` selects the level —
`outcome` (the default over MCP) empties `similars` and `related_pages`; `review` fills both.

**The defaults differ by transport, deliberately.** MCP defaults to `outcome`, HTTP keeps
`review`. This is not a compromise nobody wanted: it is two callers with different needs, and
picking one default for both would either keep the noise in front of the model or break a
shipped programmatic contract.

**At `review` detail `similars` keeps the whole ranking, rank 1 included.** `max_similarity`
duplicates its first entry there. Deduplicating would have been tidier and would have quietly
changed a shape the HTTP surface has always returned; additive is the safer trade, and the
duplication costs about eighty bytes on a path that opted into detail.

Both adapters render through `api_models.contribution_response`, as they do for search through
`search_response`, so the two cannot drift about what an outcome is.

## Consequences

The MCP write response falls from 2,241 to 700 bytes — real but modest, and it should not be
sold as the saving. Search is where the tokens were (ADR 0031). This is about removing a
prompt to act, not about bytes.

A programmatic MCP client that wanted every candidate now passes `response_detail="review"`.
None is known; the reviewing surfaces are the HTTP ones, which are unchanged.

`max_similarity` is taken as the first element of `similars` rather than recomputed with
`max()`, because the service already returns them ordered by score. That couples this
projection to that ordering. It is asserted in `tests/vault/test_mcp_budget.py`
(`max_similarity == similars[0]` at review detail), so a change to the ordering fails a test
rather than silently reporting the wrong note as the verdict.

Writing this ADR exposed a defect in the Phase 0 budget fixture: it seeded documents with no
embeddings, so `find_similar` — which joins through `vault_document_embeddings` — returned
nothing, every assertion about the gate's output was vacuously true, and the recorded
contribution budget was smaller than any real response. The fixture now embeds what it seeds.
A fixture that cannot exercise the path it measures reports comfort rather than a measurement.
