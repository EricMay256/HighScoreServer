# 0043. Librarian runs are persisted drafts, and only humans commit them

Date: 2026-09-02

## Status

**Proposed.** This records the Phase 0 design for approval before the workflow
schema, scopes, routes, MCP operations, or provider integration are implemented.

## Context

The vault has governed operations for contribution, amendment proposals, review,
compilation, promotion, and compile decline. A librarian needs to investigate the
same corpus and help a human prepare work across those operations, but it must
not turn untrusted document text or model output into corpus authority.

Consumer chat subscriptions cannot be invoked by the website as a backend. A
direct provider API can be invoked, but has a separate billing model and needs
hard application-level cost control. Both execution modes should produce the
same human-visible workflow rather than leaking provider-specific state into the
corpus or console.

The current schema already supplies several primitives that this work must reuse:
`content_revision` is the optimistic-concurrency token; compile runs own compile
lifecycle and provenance; `compile_declined_at` is the existing judgement that a
note need not be compiled; and `promotion_status` already carries promotion
work. There is no implemented `proposed_doc_type` column or workflow. The
librarian must use the live promotion-status workflow rather than designing
against that stale documentation term.

`vault_audit_events.trace_id` is available for a session correlation and
`request_id` identifies an individual request. Audit events are append-only
security history, not a substitute for resumable workflow state.

## Decision

Add an application-owned librarian workflow beside, not inside, the document
corpus. A session persists its owner, goal, runner/provider/model snapshot,
workflow revision, timestamps, optional compile-run reference, and the active
provider response identifier when applicable. Its append-only events retain only
user-visible messages, concise tool summaries, transitions, decisions, evidence,
errors, provider identifiers, usage, and cost. It never stores hidden model
reasoning.

Persist versioned, provider-neutral artifacts separately from events. An artifact
is a draft note contribution, note amendment, wiki page, metadata amendment, or
one direction of a paired link. It records source and target document IDs and
the `content_revision` values observed while drafting, payload, rationale,
evidence, preview, state, and the identifier produced after submission.

Persist typed invocations separately from events. They provide one-in-flight
enforcement per session, application-minted idempotency, provider request IDs,
status, timestamps, token usage, estimated cost, and the pricing snapshot used
for its reservation. This supports the sub-$5 monthly provider experiment
without treating an event JSON blob as accounting data.

Invocation rows are retained permanently for the initial interactive-only
workflow. The bounded step model and hard monthly budget make expected volume
small, while permanent records keep accounting, recovery, and incident review
defensible. This is a retention choice for workflow records only; it does not
replace or narrow the append-only audit-event history.

Do not introduce an invocation-pruning script speculatively. Revisit retention
when measured row growth, database cost, backup/restore time, or the latency of
the session and monthly-cost queries demonstrates a concrete operational burden,
or when a defined compliance requirement requires a lifecycle. That future work
must have its own ADR and a dry-run-first script that reports its exact target
set, preserves sufficient monthly accounting aggregates and audit correlation,
and is covered by recovery and retention tests before it can delete rows.

Every governed action writes its ordinary audit event with `trace_id` equal to
the librarian session UUID. A step, heartbeat request, or approval submission
sets that action's `request_id`. The workflow service creates idempotency keys;
the model never supplies them.

The workflow has two runners behind one application interface:

- `ExternalMCPRunner` queues a bounded step for a subscription-backed external
  agent. It is the default runner.
- `ProviderAPIRunner` calls a server-side provider through a provider-neutral
  adapter. The first adapter may use async `httpx`; no provider SDK is added
  unless maintaining the protocol proves materially worse.

New sessions snapshot their runner, provider, and model. Environment changes
affect future sessions only. A provider conversation never resumes through a
different backend merely because configuration changed.

The model receives only read and draft/runner capabilities. It cannot contribute,
review, update, delete, or compile. Human approval invokes existing application
services: contribution and deduplication for a new note; immutable amendment
proposal submission for a revision; normal compile writes for a wiki page; and
metadata amendment proposals for links, facets, and source metadata. A model
never receives `vault:review`. Documents remain untrusted model input; server
tool filtering, not a prompt, enforces the capability boundary.

The initial workflow is interactive only. `Step` advances one bounded transition.
`Heartbeat` repeatedly invokes step up to a fixed limit and stops for feedback,
approval, completion, cancellation, error, or budget exhaustion. It creates no
scheduler, worker, queue, sleeping Gunicorn task, or new Heroku process.

The scope split is `vault:librarian` in OAuth baseline scopes for a
principal operating its own sessions, feedback, artifacts, and approvals, and a
separately issued `vault:librarian-run` capability for an external runner. The
existing privileged `vault:compile` entitlement remains human-held and is never
granted to the model. OAuth scope constraints, grants, refresh tokens,
constants, CLI behavior, and drift tests must change together.

## Consequences

- Workflow state survives refreshes and provider outages without being confused
  with governed corpus content or audit history.
- Human approval remains the only route from a librarian draft to corpus state.
- External subscription-backed execution has zero incremental API cost; a direct
  provider runner remains opt-in and separately budgeted.
- Provider calls must be asynchronous. Blocking provider work on FastAPI's event
  loop is not permitted.
- The workflow schema, runners, console, MCP adapter, scope constants, tests,
  and migrations all belong to `app/vault/` or `vault_migrations/` and leave with
  the vault package.
- Implementation requires an Alembic revision and coordinated OAuth/schema-drift
  coverage. It adds no dependency for the first provider adapter.

## Alternatives considered

**Give a model existing write and review tools.** Rejected. It bypasses the
governance boundary precisely where untrusted corpus text can influence a model.

**Store librarian progress as documents or audit events.** Rejected. Documents
would enter governed knowledge lifecycle and retrieval; audit events cannot
provide compare-and-set workflow state, artifacts, or accounting.

**Use a scheduler or worker for autonomous operation.** Deferred. The initial
interaction model is intentionally bounded and client-driven.

**Make the provider API runner the default.** Rejected for the initial release.
It incurs API cost while consumer subscriptions cannot be programmatically
woken by the website; the external MCP runner matches that constraint.

## Decisions still required

- Select the first provider/model pair only after verifying current official
  pricing and limits for the sub-$5 experiment.
