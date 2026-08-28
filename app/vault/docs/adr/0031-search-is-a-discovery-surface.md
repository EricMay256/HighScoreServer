# 31. Search names candidates; fetch returns documents

Date: 2026-08-26

## Status

Accepted 2026-08-26.

Amends ADR 0006 (hybrid retrieval fused by reciprocal rank), which decided how results are
*ordered* and left open what a result *is*. Depends on ADR 0008 (search returns active only;
fetch also resolves archived) and ADR 0021 (the MCP adapter is scope-shaped). Constrains any
future chunk-level retrieval.

## Context

`vault_search` returned a complete `VaultDocumentDetail` for every hit — the identical
projection `vault_get_note` returns — plus three ranking fields. Discovery and retrieval were
the same operation with the same cost, and `vault_get_note` existed to fetch what the caller
already had.

Measured on 2026-08-26 against a ten-hit page of realistically-sized notes: **58,784 bytes**,
of which 28,277 was the structured copy and 30,507 the compatibility text block. Roughly
14,700 tokens for one search. A session that searched eight times — which the transcript that
prompted this work did — spent over a hundred thousand tokens discovering things, and threw
away nine bodies out of every ten it paid for.

Three facts sharpened the decision.

**Both wire copies already ship.** A tool annotated `-> dict[str, Any]` looks schema-less and
is not: `func_metadata` derives a permissive `{"type": "object", "additionalProperties":
true}` from it, and any derived schema is enough to make the SDK attach `structuredContent`
beside the text block. The MCP specification asks for both, for clients predating structured
output, so this is correct behaviour and not a defect. Its consequence is that every byte
removed from a response is removed twice, and that giving these tools typed returns costs
nothing in size.

**A note's `summary` is nearly always absent.** Across the corpus on 2026-08-26: 3 of 70 notes
carry one, against 14 of 15 wiki pages. So a metadata-only hit built from authored fields
alone would, for the note corpus, offer a title and nothing else.

**Nothing can explain a semantic match.** `ts_headline` highlights lexical matches and was
considered for the preview. A hit found only by the vector arm shares no vocabulary with the
query by construction, so there is nothing to highlight and `ts_headline` falls back to the
document's opening words anyway.

## Decision

**A search hit carries what is needed to choose between candidates. Everything else is a
fetch away.**

A hit is `note_id`, `title`, `summary`, `snippet`, `kind`, `doc_status`, `content_revision`,
`score`, `lexical_rank`, `vector_rank`. Removed: `body`, `tags`, `aliases`, `facets`,
`related_ids`, `source_ids`, `vault_path`, `doc_type`, `status`, `created_at`, `updated_at`.

The test a field must pass to be in a hit is *selection* value — does it help decide which
note to open. Not "is it small", and not "is it convenient". Adding one back requires a
demonstrated selection need.

### The preview is a lead extract, and says so

`snippet` is the note's opening paragraph, bounded at 320 characters, code fences and headings
skipped, an ellipsis marking a clip. It is supplied **only when `summary` is absent**; callers
read `summary or snippet`. Sending both would restate the same thing, and would do it exactly
where it is least useful, since summary coverage is concentrated in the wiki pages that
already have one.

320 was chosen against the corpus, not picked. The full opening paragraph has a median of 313
characters (p75 450, p90 769); the share arriving uncut is 34% at 240, 53% at 320, 68% at 400.
320 is the widest setting that keeps a ten-hit page inside the 8 KiB structured ceiling once
per-hit identifiers and ranking are counted.

The schema calls it a bounded preview rather than a match highlight. A field that meant "why
this matched" for lexical hits and "the opening words" for vector hits would be worse than one
meaning the same thing for both, because the caller cannot tell which arm found a given hit.

### `score` orders within one response and nowhere else

Documented on the field. A fused score is computed from positions within one query's candidate
set, so it is not comparable across queries and no fixed value means "relevant". This was
already true and unstated, which is the kind of thing a caller discovers by building a
threshold on it.

### Pagination is answered honestly or not at all

`has_more` is a fact: fusion produces the whole ranking and the page is a slice, so the
remainder is free to observe. It is bounded by the candidate window — each arm fetches
`candidate_depth(limit)` rows, capped at `MAX_CANDIDATES` — so it means "fusion ranked more
than fitted", not "the corpus contains more". For deciding whether to narrow a query those are
the same answer.

`next_cursor` is **reserved and always null**. Resuming a page needs a total order stable
between calls, and this ranking has none: inserting a document can move every score below it,
and a document outside the candidate window has no score at all. A cursor here would be an
offset in disguise, promising a stability the ranking cannot deliver.

`truncated` reports that hits were dropped to fit a byte budget — distinct from `has_more`,
which is about the ranking. The response is trimmed from the tail, because fusion has already
ordered the hits and the lowest-ranked are the ones worth losing. At least one hit always
survives, which makes the budget best-effort rather than a hard ceiling: the surviving hit may
exceed it alone. The measurement is of the whole response, envelope included, because the
echoed `query` and `profile_id` are part of what is sent.

### One builder, both adapters

`api_models.search_response` assembles the response and both `routes.py` and `mcp.py` call it,
for the reason `canonical_request_digest` is shared: two copies of a response contract drift,
and a search that means different things over HTTP and MCP is a difference nobody notices
until a client depends on it.

## Consequences

A ten-hit page costs **10,650 bytes** (structured 4,859, text 5,791) against 58,784 before —
an 82% reduction, with the structured copy inside the 8 KiB ceiling with room to spare.

The HTTP surface changed shape too, and that is a breaking change to a public contract. It was
taken because no consumer outside this repository exists — `scripts/vault_load_probe.py`, the
docs and the tests are the callers — and because leaving the adapters divergent would cost the
invariant that they are two thin adapters over one service.

A caller that genuinely needs a removed field now makes a second call. That is the intended
trade and it is not free: a client wanting `tags` for all ten hits is worse off than before.
None is known to exist, and the fetch is one call rather than ten because the ordinary case
fetches one or two notes.

`vault_search` gained `ToolAnnotations` marking it read-only, non-destructive, idempotent and
closed-world, and a typed return, so its output schema now describes the response instead of
permitting any object. The remaining thirteen tools still report `annotations: null` and a
permissive schema; `tests/vault/test_mcp_contract.py` pins that so the gap is visible.

The snippet is computed in the application from bodies the hydration query already loads.
Bodies therefore still cross the database boundary to be discarded at the transport boundary.
That is deliberate: the cost being fixed is application-to-model, not database-to-application,
and `ts_headline` would trade a local socket read for a per-row re-parse of the document text
in Postgres plus a coupling to the text-search configuration. Revisit if the corpus grows
enough for hydration to matter, or if lexical highlighting proves worth having on its own.

### What a `ts_headline` arm would actually require

Amended 2026-08-26, after measuring the options rather than reasoning about them. An earlier
draft of this section said the fix was to pass `ts_headline` "only the terms the lexical arm
scored on". **That is a no-op**, and the correction is worth recording because it is an easy
mistake to make twice: a term absent from a document contributes nothing to `ts_headline`
already, so restricting the query to the matched subset is the same query from its point of
view. Measured against one note, `'stop' | 'retri' | 'creat' | 'duplic' | 'note'` and `'retri'`
produce byte-identical fragments.

The real distortion is which matched term is allowed to *anchor* the fragment. ADR 0007
rewrites the tsquery's conjunctions to disjunctions, which is right for ranking — `ts_rank_cd`
and RRF then decide how much a shared word is worth — but `ts_headline` has no ranking to fall
back on and weighs every lexeme equally. So a note that shares one incidental word with a long
question gets a fragment centred on that word:

> turns a network timeout into a second write, because the server cannot tell the **[retry]**
> from a fresh submission.

against the lead extract's "An idempotency digest must depend on the request alone." Dropping
the weak term and passing `'idempot' | 'duplic'` anchors it correctly, which identifies the
lever: an information cut, not a match filter.

**The two halves of that design are not separable, which is the finding.** Routing by
retrieval arm — headline the hits with a `lexical_rank`, lead-extract the rest — looks like the
cheap half, because the ranks are already computed. It does not help: the note above *matched*
lexically, so the routing rule sends it to `ts_headline` and it comes back worse than what is
shipped today. Any headline arm needs the weak-term cut from the start or it regresses exactly
the queries this corpus receives.

### Why the weak-term cut is not being built

`ts_stat` supplies document frequency, and on the live corpus (75 active notes, 2,324 distinct
lexemes) **113 lexemes appear in more than 20% of documents** — `one(49)`, `run(47)`,
`two(44)`, `read(39)`, `check(38)`, `write(35)`, `path(33)`. Those are the anchors to drop. It
is buildable. It is not worth building yet:

- **The stop-list is derived state that goes stale.** It has to be recomputed as the corpus
  grows or it silently drifts, which is a new maintenance surface in exchange for a display
  field.
- **75 documents is too few for the statistic to mean what it claims.** `path(33)` is frequent
  in *this* corpus, but a note genuinely about path handling is precisely the one a path query
  should highlight. At this size the measurement describes the corpus's topic distribution
  rather than term informativeness.
- **It would duplicate a judgement the ranker already makes correctly.** `ts_rank_cd` weights
  rare terms above common ones; a hand-rolled cut is a second, cruder copy of that, for
  display only.
- **The fallback is measurably good.** The lead extract produces a preview for 85 of 85
  documents, median 213 characters, and these notes are written thesis-first — so the opening
  sentence is close to an authored summary nobody had to author. The comparison above is one
  where the lead extract simply *wins*.

The gate for revisiting: evidence that lexically-found hits are being previewed badly. That is
measurable without shipping anything — generate both a headline and a lead extract for the same
hits and count how often the headline is better. Build the arm only if it wins often enough to
pay for the stop-list.
