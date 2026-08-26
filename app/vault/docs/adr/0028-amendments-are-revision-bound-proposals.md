# 28. Amendments are revision-bound proposals, not notes

Date: 2026-08-24

## Status

Accepted 2026-08-24; amended 2026-08-25; implemented with migration 0016.

Amends ADR 0020's scope vocabulary and applies ADR 0026's scope-shaped tool boundary to
editing established knowledge.

## Context

The contribution surface can only add a note. The update surface can improve one, but it is
a privileged full replacement under `vault:update`; giving that tool to every agent would let
instructions read from the corpus spend the session's legitimate authority to overwrite the
corpus. The compiler can rewrite wiki pages under `vault:compile`, but that scope deliberately
means synthesis, not source-note curation.

New contributions often belong in an existing note. Making every such addition a new note
fragments an insight across near-duplicates and leaves the compiler to rediscover the intended
merge. Making a “Proposed Amendment” document kind would be worse: search and RAG could serve
unaccepted text as knowledge, and deduplication would score established notes against workflow
state.

The direct update path is also last-write-wins. An amendment composed from revision A must not
silently replace revision B merely because review happened later.

## Decision

**An amendment is an immutable workflow record carrying a change, a rationale, and the content
revision it was composed from. It is not a vault document.** It is absent from search,
embeddings, deduplication, compilation and export until accepted.

There are two closed change kinds:

- `replacement` carries every caller-controlled content field. It is required for metadata
  changes and body changes too large for the compact form.
- `body_diff` carries a strict unified diff against only the body. It may add, edit, or remove
  lines. Every hunk must anchor itself with exact context or exact removed text; mismatched
  context, inaccurate coordinates/counts, overlapping hunks, and shell execution are refused.
  The compact form is bounded to 50,000 patch characters, 20 hunks, 200 changed lines, and 25%
  of the existing body's line count, with a 20-line allowance for short notes. A change above
  any limit must use `replacement`.

There is no separate additive-only kind. It would add a second API concept without changing
the authorization boundary: proposals cannot mutate corpus content, while review can apply an
immutable proposal. The compact representation therefore follows the correction use case as
well as the addition use case.

`vault_documents.content_revision` starts at 1 and increments whenever caller-supplied content
is replaced. Lifecycle judgements such as review, promotion and compilation decline do not
increment it. A proposal stores `target_revision`; acceptance compares it under the corpus
advisory lock. A missing target or mismatch settles the proposal as `stale` and writes nothing.

Proposal submission is `vault:propose`, a distinct OAuth-baseline verb. It materializes the
candidate against the exact base revision and validates the resulting document in the target's
governance context, but performs no embedding call because it does not change the corpus. The
stored payload remains compact for body diffs; the service reapplies and revalidates the exact
diff on acceptance rather than storing a hidden expanded replacement.

Adjudication is `vault:review`. A reviewing credential remains `vault:read + vault:review` and
nothing else. It may accept or reject a stored proposal but cannot author a different payload;
acceptance applies exactly what the proposer stored, through the update path's validation,
embedding and dedup gates. Applying the change, storing its vector, settling the proposal
and writing the audit event are one transaction.

Reading a proposal materializes three review views against the exact base revision: the complete
resulting body, a canonical unified diff, and a removal summary naming every removed line and
its original line number. A stale proposal has no preview because applying its patch to newer
content would imply a rebase. Acceptance of any proposal whose body removes lines—including a
full replacement—requires `acknowledge_removals=true`. The acknowledgement is persisted on the
settled proposal; it is not inferred from a generic accept decision.

`vault:compile` remains wiki-page authority. A librarian may run a separate curation phase with
a review credential, but compilation itself does not gain source-note overwrite authority.

Proposal target ids are durable correlations rather than foreign keys. Retirement may remove a
target; the proposal remains as history and a later acceptance settles stale. Queue listing
omits change bodies, while reading one selected proposal exposes the untrusted content
needed to judge it.

## Consequences

- Ordinary agents can suggest consolidation without gaining `vault:update`.
- Focused body additions, corrections, and removals cost only changed text plus anchoring
  context. Metadata changes and large rewrites retain the full-replacement form.
- Review responses are larger because responsible review includes the complete materialized
  body. Token savings accrue to proposal authoring, not by hiding context from adjudication.
- Removals require a second explicit reviewer signal and leave durable evidence of that
  acknowledgement.
- RAG and deduplication see only endorsed content.
- Review cannot lose a concurrent edit; stale proposals must be rebased into a new proposal.
- Acceptance requires the embedding provider, while rejection and proposal submission do not.
- `vault:propose` joins the OAuth baseline, so newly authorized clients can request it; existing
  tokens retain their original scopes until reauthorization or refresh from a widened grant.
- The service gains a table, an enum, a document revision column, REST routes, MCP tools, quota
  buckets and an Alembic revision. Proposal history is durable, so downgrade refuses while any
  proposal rows exist.
- Direct `vault:update` remains available for deliberate operator/importer maintenance and
  remains last-write-wins; the safe collaborative editing surface is the proposal workflow.
