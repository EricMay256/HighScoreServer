# 0044. Tweaking a proposal creates a linked successor

Date: 2026-09-02

## Status

**Accepted 2026-09-02.** The proposal-lineage migration and reviewer-side
editing flow remain to be implemented.

## Context

Amendment proposals are immutable, revision-bound workflow records (ADR 0028).
The review console can decide a pending proposal but cannot improve it. ADR 0039
deferred reviewer-side editing because the reviewer credential is deliberately
`vault:read` plus `vault:review`, while proposal authors use a separate
`vault:read` plus `vault:propose` credential.

A reviewer should be able to correct a nearly-right proposal without rewriting
the filed record or collapsing those credentials into one authority. The existing
span form materializes a canonical body diff from an exact selection, but its
stored result remains an ordinary amendment proposal. Metadata is structured and
must not be treated as a text span.

`vault_review_cases` reserves `superseded`, but amendment proposals currently
have only `pending`, `accepted`, `rejected`, and `stale`. The review-case value
is not a reusable amendment-proposal state.

## Decision

Create an immutable successor amendment proposal and link it to the proposal it
revises. The proposed schema is a nullable `revises_proposal_id` on
`vault_amendment_proposals`; `revised_by` is derived through the reverse
relationship rather than duplicated in a second column.

`revises_proposal_id` is an unconstrained durable correlation, not a
self-foreign-key. Proposal history must survive independently of the target
note and must not become undeletable merely because another proposal names it.
The service validates the predecessor at successor creation time; the durable
identifier preserves the relationship after either record is later unavailable.

Add an amendment-specific `superseded` state. After the successor has been
created successfully, the reviewer settles the predecessor as `superseded` with
a decision note naming the successor. It means "rejected because a replacement
was filed", not the generic rejection of an unsuitable proposal. Queue,
reporting, API, and settlement behavior must make that distinction visible.

For a body edit, the review page materializes the pending proposal's resulting
body, lets the reviewer adjust a selected span, previews the adjusted result and
canonical diff against the target note, then asks the complementary proposer
credential to file the successor. Touching title, body, tags, aliases, or any
other embedded field uses the full replacement form and therefore follows normal
re-embedding when accepted. Facets, links, and other non-embedded metadata use
structured controls and a metadata amendment.

This is deliberately a two-credential, two-step browser flow rather than one
database transaction:

1. The proposer credential files the successor against the unchanged target
   revision with `revises_proposal_id` and an application-owned idempotency key.
2. Only after that succeeds, the reviewer credential rejects the predecessor
   with its replacement decision note.

If filing fails, the predecessor remains pending. If the successor is filed but
settlement fails, the review page exposes a recoverable "successor filed; finish
settlement" state. A retry replays or discovers the same successor before any
new proposal is created. The predecessor is never settled first. A target whose
`content_revision` changed is refused rather than silently rebased.

## Consequences

- The original proposal, successor, author, reviewer decision, and recovery
  state remain inspectable.
- The existing separation of duties survives: a reviewer decides while the
  complementary proposer credential authors the successor.
- The review page needs two independently refreshed OAuth sessions and must make
  missing or expired proposer authority actionable rather than assuming it.
- Proposal lineage requires an Alembic revision, Core table metadata, domain/API
  models, repository and service methods, REST routes, browser JavaScript, and
  recovery/idempotency tests.
- Amendment proposal state gains a visible `superseded` outcome. The migration,
  API models, queues, reports, and settlement logic must consistently treat it
  as a settled state distinct from generic rejection.

## Alternatives considered

**Rewrite the original proposal.** Rejected. It destroys the filed artifact and
contradicts ADR 0028's immutability and revision-bound review model.

**Let the reviewer credential author the replacement.** Rejected. It combines
the authority to compose and decide into one credential, defeating ADR 0021 and
ADR 0039's separation boundary.

**Use `rejected` plus lineage.** Rejected. It obscures that the proposal was
replaced rather than found unsuitable, making both human review and reporting
needlessly ambiguous.

**Use a self-foreign-key.** Rejected. It couples proposal retention to a linked
workflow record and would prevent deleting a predecessor even when its durable
correlation is sufficient for history and reporting.
