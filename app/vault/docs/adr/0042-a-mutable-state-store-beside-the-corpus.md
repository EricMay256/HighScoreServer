# 0042. A mutable state store beside the corpus

Date: 2026-08-30

## Status

**Considered, not scheduled.** A design worth having written down, deliberately
not on the near-term list. The bridge in "Testing it cheaply" is the part to do
first, and it may be the only part worth doing.

## Context

The vault holds durable, reusable insight. It holds nothing about *where an
agent is*: what it already tried, which fix failed, what it was part-way
through when a session ended. That gap has a visible cost — an agent
re-deriving context it had last week, or re-running a fix it already watched
fail.

The prompt for this came from *SKILL.state* (arXiv 2608.26263), which argues
that agent runtimes should stop appending a transcript and instead hand the
model a structured JSON state it patches each turn, discarding the reasoning.
Its most interesting result is not the token count: with every runtime pinned
to the same ~1,800-token budget, sliding-window truncation scores 0.18,
entropy-based compression 0.22, and the structured state 0.94. **Structure, not
brevity, is doing the work** — a statistical compressor prunes identifiers it
cannot tell from filler.

Provenance worth recording: this ADR was written from a detailed summary of
that paper rather than the paper itself, which could not be read in the working
environment. The characterizations above are therefore second-hand. The paper is
also a preprint days old, unreviewed, with no released benchmark, and its
own Environment 2 — entangled dependencies, the closest analogue to a codebase
— is where its method performs *worst*, losing to a stateful baseline at T=25.

## What it would be

A mutable state store as a sibling of the document corpus inside `app/vault/`:
its own table and endpoints, sharing authentication, scopes, audit and the MCP
surface, and sharing **none** of the note machinery.

- **Its own scope, `vault:state`.** Not `vault:write`, which would make every
  contributor credential a state writer.
- **Append-only patches, periodically compacted.** Each write is a recorded
  patch with its author, compacted into a materialized blob on a cadence. This
  is the design's best feature: it gives attribution, recoverability and an
  audit trail, and a bad write becomes inspectable and revertible instead of a
  silent overwrite.
- **Facets and tags for filtering, on its own endpoint.** Never embedded, never
  in note search. Embedding a document that changes every few minutes spends a
  provider call per patch and puts churn into the vector space the dedup gate
  scores against; and a state blob is not something anyone should discover
  while looking for an insight (ADR 0031).
- **Keyed by project and a schema name**, so a build agent and a curation agent
  hold separate state rather than contending for one blob, with each patch
  recording who wrote it.
- **Merge-patch semantics**, reusing what already exists: `VaultMetadataChange`
  is a sparse JSON merge patch with null-means-delete, and `expected_revision`
  plus `CORPUS_LOCK_KEY` already provide compare-and-set and serialization.

Every one of these choices exists to keep State *out* of the governed note
path. That is the tell worth reading: the design is defined mostly by which
invariants it must not inherit.

## Why it is not on the short list

**It does not deliver the result that motivates it.** The paper's win comes from
the *runtime* putting the state in the prompt instead of the history. A store
can supply that state; it cannot make any MCP client stop accumulating its
transcript. What this would actually deliver is cross-session persistence and
cross-agent coordination — genuinely useful, and a materially smaller claim
than the one the paper supports. Building it while expecting the token and
accuracy curves would be building it for the wrong reason.

**It is an unreviewed channel from one agent into another's context.** The
vault's central safety property is that content capable of steering an agent
passes a human first (ADR 0021), which is why `vault:propose` writes inert
proposals and only `vault:review` applies them. Shared mutable state cannot have
that gate and remain useful. The patch log gives attribution *after* the fact,
which is worth having and is not the same thing as a gate before. If this is
ever built, that tradeoff belongs in its own ADR as an accepted cost with a
stated bound — constrained value types rather than free prose is the obvious
candidate — rather than discovered in use.

**It widens a bounded context that is being prepared to leave.** `AGENTS.md`
records that `app/vault/` and `vault_migrations/` are maintained separately and
will be moved out, and the package's definition is durable, reusable notes.
State is neither durable nor reusable-as-knowledge. Adding a second domain now
makes the extraction larger and the definition blurrier.

**The schema would be guessed.** No agent has asked for this, so its fields
would be invented from a benchmark rather than observed from use — which is
precisely failure mode (a) in the paper's own limitations: the design assumes a
schema known in advance.

## Testing it cheaply

The hypothesis is testable with no vault machinery at all: give an agent a small
structured state blob at session start, have it patch a plain file, and see
whether it stops re-deriving context and re-trying failed fixes.

`attempted_fixes` is the field to trial first. It is the analogue of the paper's
`tested_hypotheses`, which their own error analysis identifies as load-bearing,
and its absence has an observable cost already.

A week of real use answers the two questions that matter and cannot be answered
by design: whether the effect is *felt*, and what the fields actually turn out
to be. Building the governed shared store afterwards means designing against
observation instead of a benchmark whose most code-like environment contradicts
the result.

## What would still have to be decided

Held here so a later build starts from questions rather than assumptions:

- Whether values are typed and constrained or free prose — the main lever on
  the injection concern.
- Whether an agent may read another's state, and whether reading it is
  advisory or authoritative.
- Retention. State goes stale by nature, and nothing here proposes a lifecycle;
  without one the store becomes a graveyard whose entries an agent still trusts.
- Compaction cadence, and whether a compacted blob is authoritative or a cache
  over the patch log.
- How a bad patch is reverted, and by whom.

## Revisit when

The cheap experiment shows a felt improvement and has produced a schema from
use; or a second agent genuinely needs to coordinate with a first through
something other than notes; or the same request arrives twice from a real
workflow rather than from a paper.
