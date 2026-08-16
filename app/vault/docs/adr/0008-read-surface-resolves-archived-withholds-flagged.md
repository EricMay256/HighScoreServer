# 8. The read surface resolves archived documents and withholds flagged ones

Date: 2026-07-29

## Status

Accepted

## Context

`vault_documents.status` is `('active', 'flagged', 'archived')`. The three mean different
things. `active` is endorsed content. `archived` is retired but legitimate — superseded notes,
material deliberately taken out of circulation. `flagged` is what the write path's policy
declined to auto-accept, the state that pairs with `vault_review_cases` and with the `flagged`
state of `vault_write_requests`; ADR 0004 leaves `vault_contrib.core.decide()` normative for
producing it.

The read-only slice shipped two ways to reach a document, and they disagreed. Both search arms
filter to `status = 'active'`. `VaultDocumentRepository.get_by_id` filtered on nothing, so
`GET /api/vault/documents/{id}` returned a document in any status. Nobody chose that: it is what
the two queries happened to do, and the difference between them was one `WHERE` clause that only
one of them carried.

Three facts bound how much this matters, and it is worth stating them plainly rather than
overstating the problem:

- **Nothing can produce a non-active document today.** The write path is not built. Only tests
  create a non-active row. This is a latent rule, not a live leak.
- **`vault_documents.id` has no format constraint** beyond `CHECK (btrim(id) <> '')` — the
  `^[A-Za-z0-9_-]{8,64}$` check belongs to `vault_agent_credentials`, a different table. IDs are
  whatever the author assigns. Every one that exists today is a hand-written slug, and a
  path-derived importer would make them straightforwardly guessable, so unguessability is not
  something this design may lean on.
- **`status` is in the response.** `VaultDocumentDetail` carries it, so a caller fetching a
  flagged document is told that it is flagged.

That last point is the real counter-argument to changing anything, and it is why this was worth
deciding rather than assuming. It fails on the consumer: the vault exists to be read by an agent,
and an agent handed a document body is exactly the caller that will not branch on a status field
before using it. Correctness that depends on a well-behaved consumer is not correctness.

## Decision

**Search returns `active` only. Fetch-by-ID resolves `active` and `archived`, and withholds
`flagged` as a 404.**

The two surfaces do different jobs, so they differ by exactly one status, deliberately:

- Search is **discovery**. Retired content should not be surfaced to someone who did not ask for
  it specifically.
- Fetch-by-ID is **resolving a reference the caller already holds**. `related_ids` and
  `source_ids` are text arrays with no foreign key, and they will point at superseded documents
  as the corpus ages. A reference that dead-ends at 404 is worse than one that resolves to
  content marked `archived`.
- `flagged` is withheld from both, because "the policy declined to endorse this" is not a
  caveat to attach to a payload — it is a reason not to send one.

**The restriction lives at the route, not in the repository.** `get_by_id` stays unfiltered by
default and takes an optional `statuses` argument; `routes.READABLE_STATUSES` states the read
surface's rule in one place and passes it in.

This is the load-bearing half of the decision, and it is the opposite of what "just add the
`WHERE` clause next to the other one" would give. Which statuses a caller may see is a property
of the **surface**, not of persistence:

- Review tooling must be able to load a flagged document *precisely because* it is flagged. A
  repository that filtered by default would put the write path in conflict with the read path
  over a shared method, and the likely resolution — a second method, or a boolean that means
  "actually show me everything" — is worse than naming the policy where it applies.
- A default of "unfiltered" is honest about what the storage layer knows. It knows how to fetch a
  row by primary key. It does not know who is asking.
- Keeping it a caller-supplied argument means the next surface — review UI, export, compile —
  states its own rule explicitly rather than inheriting one by accident, which is the failure this
  ADR exists to correct in the first place.

## Consequences

The two read paths now differ by an explicit constant rather than by an omission, and the
difference is one line to read.

`flagged` becomes unreachable through the public API. That is intended, and it means the review
surface, when it is built, cannot be a thin wrapper over these routes — it needs its own
authorization and will call `get_by_id` with its own status set. The repository already supports
this; no change is owed to it.

Archived documents remain fully readable by anyone holding the read key, including their body.
"Archived" is a visibility state, not a privacy one. Content that must not be read after
retirement needs deletion or a genuinely separate mechanism, and nothing here should be mistaken
for one.

The rule is recorded as an invariant in `app/vault/AGENTS.md` so the write path inherits it, and
pinned by tests: archived resolves, flagged 404s, and the repository stays unfiltered unless a
caller restricts it.

`READABLE_STATUSES` is a module constant rather than configuration. Making it settable would let a
deployment quietly opt into serving unendorsed content, which is the decision this ADR is making
centrally and on purpose.
