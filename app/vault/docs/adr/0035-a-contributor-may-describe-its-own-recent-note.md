# 35. A contributor may describe its own recent note

Date: 2026-08-26

## Status

Accepted 2026-08-26.

Amends ADR 0028 (amendments are revision-bound proposals), which routed every edit by a
baseline credential through review. Refines ADR 0032 (a contribution reports its verdict) by
adding one field to the write response. Depends on ADR 0013 (what the embedding text carries),
ADR 0016 (`contributed_by` comes from the credential), ADR 0024 (`vault:update` is off the
OAuth baseline) and ADR 0031 (`summary` is the search preview).

## Context

`summary` is nearly unpopulated. Measured 2026-08-26 over the live corpus: **3 of 70 notes
carry one, against 14 of 15 wiki pages.**

It is not a display field. It joins the embedding text (ADR 0013) and the `search_vector` at
weight B, so 96% of notes are giving up a ranking signal. Since ADR 0031 it is also the search
preview, with `snippet.lead_snippet` falling back to the note's opening paragraph when it is
absent. That fallback works — 85 of 85 documents produce one — but a lead extract is the
opening of a note, and an authored precis is the whole of it. The gap is widest exactly where
it hurts most: a long note whose first paragraph does not represent it.

Two separate things kept the field empty, and fixing either alone would have left the other.

**Nothing asked for it.** The `knowledge-vault` skill's "fields that need judgement" list names
`title`, `body`, `tags` and `facets`, and does not mention `summary` at all. The
`vault_contribute` tool description called it an *"Optional one-line precis; contributes to
matching"* — which undersells the retrieval role and, by saying "one-line", actively
discourages writing a real one. An agent following both faithfully would omit it every time,
which is what the measurement shows.

**Nothing could fix it afterwards.** This is the part that made the gap self-perpetuating. The
general edit path is `vault:update`, which ADR 0024 keeps off the OAuth baseline by design:
a client can never request it, because an agent holding replacement authority over the corpus
is an agent that instructions read *from* the corpus can spend. So a contributor that omitted
a summary had exactly one route back — `vault:propose`, an amendment proposal under ADR 0028.
And ADR 0028 requires the `replacement` change kind for metadata changes, so it is the *full*
form: every content field restated, bound to a content revision, stored as an immutable
workflow record, waiting for a second credential holding `vault:review` to adjudicate it.

That is the correct workflow for editing established knowledge. It is a wildly
disproportionate one for an agent adding a precis to a note it wrote thirty seconds earlier,
and its real effect was not careful review — it was that nobody ever did it.

### What was considered and not done

**Make `summary` required on contribute.** The strongest guarantee, and rejected on two
grounds. It rejects contributions that would otherwise land, at the one moment the agent has
the most context and the least ability to retry cheaply; and it would break the Stage-A
contributor outright, which cannot supply the field at all — `vault_contrib.models.Note` has
no `summary`, only `WikiPage` does. That asymmetry is very likely the *root* of the measured
one: the note schema on the authoring side never carried the field, so 14 of 15 wiki pages
have a summary and 3 of 70 notes do. A required field on a write path buys coverage by
refusing knowledge, which is the wrong trade for a corpus whose whole purpose is to
accumulate it.

**Report the absence and leave it there.** Cheaper, and worse than it looks. For a baseline
credential the advice would name a repair the caller cannot perform, so the reasonable next
move it invites is an amendment proposal — a review-queue item per undescribed note. That is
precisely the shape of argument ADR 0032 used to strip `similars` and `related_pages` off this
same response: the problem with a field is not its bytes, it is what it invites.

**Put a grace period on the existing update path.** Rejected because `VaultDocumentUpdateService`
is a full content replacement. A time-boxed carveout onto it would hand every baseline
credential the ability to rewrite the body, title and tags of its own recent notes — which
permits content laundering: contribute something innocuous, replace it once it is in the
corpus.

## Decision

**A principal may supply the `summary` its own note was contributed without, under
`vault:write`, for fifteen minutes, and only while that field is empty.**

`vault_set_summary` over MCP and `POST /notes/{id}/summary` over HTTP. Three conditions, each
answering a different objection, and all three enforced in the `UPDATE` predicate rather than
checked and then written — the operation is defined by a window, so a caller that checked
first and wrote second would have a race inside it.

### One field, not a small replacement

The request model carries `summary` and nothing else. The body, title and tags are not
reachable from this operation by construction rather than by validation, and the candidate is
assembled from the stored row with only that one value substituted. This is what stops the
laundering case, and it is why the operation can sit under `vault:write` at all: it does not
carry the authority `vault:update` means, so granting it is not granting a slice of that.

A sub-resource rather than `PATCH /notes/{id}` for the same reason. A partial update of the
note would imply the other fields are addressable here.

### It shares `vault:write` rather than taking a verb of its own

`app/vault/AGENTS.md` says scopes are verbs, one per route, and warns against gating a new
write route on an existing scope "because adding one looks like ceremony — that is exactly how
`vault:write` came to mean *may destroy any note*". That rule is right, and this is the
exception it does not cover, so the test it turns on is worth stating rather than leaving to
whoever reads the code next.

The rule's concern is capability creep: a verb quietly accumulating powers an operator never
agreed to hand over. **This route adds no capability to `vault:write`.** A contributor can
already put any summary it likes on its own note — at contribute time, in the same call, with
no extra grant. The carveout gives it a second *moment* to do the thing it could always do,
not a new thing to do.

That makes a hypothetical `vault:summarize` unusable in both directions. It could never be
usefully withheld from a contributor, because withholding it prevents nothing: the holder just
has to get the summary right on the first call. And it could never be usefully granted without
`vault:write`, because the only notes it can reach are ones that credential contributed. A
scope that cannot be meaningfully granted or withheld is not an authorization boundary; it is
a second name for one that already exists.

It was built the other way first, with its own scope and a migration widening four CHECK
constraints. Two things settled it. The security property bought was approximately nil, for
the reason above. And the cost was not just the migration: a new scope ships the feature
**off** for every credential already issued, so an operator would have had to reissue or widen
each one before any agent could use it — while the advisory stayed silent for the rest. A
change whose whole purpose is to close a measured gap should not begin by being unreachable to
the fleet that has the gap.

The test that keeps this from becoming the slide the rule warns about: **a route may share a
verb only when it can reach nothing its holder could not already have written.** Anything that
touches a field the grant did not already cover — the body, the title, another principal's
note — needs its own verb, and the answer is no less firm for the route being small.

### Only where the field is empty

The operation is **monotonic**: it moves `summary` from absent to present and can do nothing
else. A note that already describes itself cannot be made to describe itself differently, so
the corpus cannot be misrepresented through this door — only completed.

The cost is real and accepted: an agent that writes a poor summary cannot correct it here, and
must propose an amendment like anyone else. Monotonicity is worth more than that convenience,
because it is what makes the carveout simple enough to reason about. A door that can only add
needs a much weaker argument than one that can also overwrite.

### Only the caller's own note, and only inside the window

`contributed_by` is authorization-grade: ADR 0016 takes it from the credential and never from a
request body, precisely so that the actor in the audit trail is the authenticated one. The
predicate compares it against the same `agent:{principal_id}` the contribution path stores.

The window is the condition doing genuine security work, and it is worth saying what it buys,
because "the principal could have written this at contribute time" is otherwise a complete
argument for not having one. It is not complete. `summary` is embedded and previewed, so a
crafted one is a **retrieval-poisoning** vector: it can make an unrelated note surface for a
targeted query, and it does so in the field a searcher reads to decide what to open. Bounding
the carveout in time means the principal amending the note is the one that just wrote it, in
the same working context — not one that has since read hostile instructions out of the corpus,
a document, or a web page.

**Fifteen minutes**, and not configurable. It covers the shape the gap actually has —
contribute a few notes, then tidy up at the end of a task — while staying far short of a
session. A deployment that widened it through an environment variable would be relaxing an
authorization boundary by configuration, which is the mistake `PENDING_AUTHORIZATION_TTL_SECONDS`
is kept out of settings to avoid.

Three misses collapse into one 404: no such note, not a note, and not yours. Separating them
would let a caller enumerate authorship across the corpus, which is the disclosure ADR 0014
closes on the read surface and ADR 0016 closes on the dedup query. The two refusals a caller
*has* earned — already summarized, and window closed — say so plainly, because by then it has
proved it wrote the note.

### The dedup gate still runs

Changing the summary changes the embedding text, so the note is re-embedded and
`embedded_text_sha256` moves with it in the same transaction. Nothing in this codebase repairs
a stale hash after the fact, so a path that wrote one without the other would leave a vector
permanently describing text nobody embedded.

And the write goes through `decide()` under the corpus lock like every other. A carveout is not
an exemption from the vault's one invariant; it is a narrower door through the same wall.
`VaultDocumentUpdateService` is explicit that a surface skipping the gate would be the easy way
around it, and "it is only one field" is exactly the argument that would erode it. Like an
update and unlike a contribution, a collision **refuses**: the note is already active and
readable, and taking it out of the read surface as a side effect of describing it would leave
the caller worse off than never having called.

`content_revision` moves, because a summary is caller-supplied content. An amendment proposal
composed against the previous revision therefore goes stale rather than silently applying over
the new summary (ADR 0028).

### The contribution response names the repair

`VaultContributionResponse` gains `summary_advice`: present only when a note landed without
one, null otherwise. It is an instruction rather than a complaint — it names the call and the
window that call stays open for.

**It is one sentence, and the brevity is measured rather than stylistic.** This fires on
almost every contribution the corpus currently receives, and ADR 0032 established that every
byte on this response is paid twice, in `structuredContent` and in the text block. A first
draft that also explained *why* a summary matters put the write response at **1,562 bytes
against the 1,024 `test_mcp_budget` pins — 153% of budget**. The reasoning moved to the
`vault_set_summary` description, which a model reads once when deciding to call the tool,
rather than riding along on every write. The note id is not repeated either: `note_id` is
already a field of this response.

Only on `inserted`. A `flagged` note is written but withheld from the read surface pending
adjudication (`READABLE_STATUSES` is active and archived), so the carveout would 404 on it, and
advice naming a call that cannot succeed is worse than no advice. `rejected` and `invalid` wrote
nothing. A replay is left alone because it reports what an earlier request did.

The *rule* lives in the shared builder that both adapters call, following ADR 0031's one-builder
principle; the *vocabulary* comes from the adapter, because a tool surface and an HTTP surface
do not share names for the same operation.

### Intake changes with it

The carveout repairs; it is not meant to be the ordinary path. The skill's editorial bar gains
`summary` as a field that needs judgement, and the `vault_contribute` description says what
the field actually does instead of calling it an optional one-liner.

**Stage A is left alone, and that is a known gap rather than an oversight.** Giving the
Stage-A CLI a `--summary` flag is not a flag: `Note` has no such field, so it would take a
dataclass change, a frontmatter key, a governance-schema entry for the Agent Note type and a
note schema-version bump, in a separate repository with its own ADR lineage. The skill
therefore marks `summary` **service-only**, exactly as it already marks `facets`. Closing it
properly belongs to `knowledge-platform`.

## Consequences

- An agent can describe its own note without holding `vault:update` and without opening a
  review-queue item. The measured gap has a repair path that costs one call.
- `vault:write` now gates two routes rather than one, which is a departure from one-verb-per-
  route and is argued for above. It is the first row-level, time-bounded authorization in this
  codebase, bounded by field, by emptiness, by authorship and by time. Anything that extends it
  should have to argue past all four bounds, not three, and past the sharing test as well.
- The review queue does not fill with one-field proposals, which is what the honest alternative
  would have produced.
- A note left undescribed past the window stays undescribed until an operator backfills it or
  someone proposes an amendment. That is the intended asymmetry: repair is cheap while the
  author is still there, and deliberately not cheap afterwards.
- **No Alembic revision.** `summary` is already nullable and `created_at` already exists; the
  carveout is authorization logic over columns the schema has carried since the baseline.
- The MCP surface gains a fifteenth tool, and `tests/vault/mcp_surface.json` moves with it.
- The HTTP contract gains a route and one additive response field. `summary_advice` defaults to
  null, so no existing consumer sees a change in shape.
