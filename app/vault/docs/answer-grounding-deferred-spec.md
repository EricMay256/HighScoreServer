# Vault answer grounding: deferred feature specification

Date: 2026-09-16
Status: Candidate designs and implementation gates; not approved schema or API contracts.

## Purpose and order

Extend the [immediate grounding protocol and evaluations](answer-grounding-now-spec.md)
when observed use requires durable source records, precise evidence relationships,
or cheaper retrieval. Start with the immediate phase's failure reports and transcripts.
Do not infer a requirement for every feature below merely because an external
project implements it.

Suggested dependency order: source identity/versioning (D1), then claim evidence
(D2), then evidence-aware maintenance (D4). Batch retrieval (D3) and retrieval
experiments (D5) can proceed independently when their measurements justify them.
Each material architectural choice needs a Nygard ADR under this directory's `adr/`
lineage before implementation. Assign the next available number at that time.

## D1. Structured source registry and immutable observations

Trigger: evaluated answers repeatedly need provenance the current single
`source_url` and document lineage cannot express, such as several external sources,
source changes over time, retrieval dates, or reusable source authority metadata.

Recommended candidate design: SQLAlchemy Core records in the vault schema, owned
by the service and projected into exports. Table names are provisional:

| Record | Purpose and candidate fields |
| --- | --- |
| `vault_sources` | Stable source ID, kind, canonical locator, title, authority classification, declared independence group |
| `vault_source_observations` | Source ID, observation ID, observed time, upstream version/commit where available, content hash, capture locator, retrieval outcome, review/refresh metadata |
| `vault_document_sources` | Document ID, observed document revision, source observation ID, optional precise locator |

Source identity and observed bytes must be separate: changed bytes create another
observation of the same source. A hash identifies captured bytes, not factual
accuracy. Decide canonicalization by source type; preserve URL components that can
change resource meaning. Never automatically fetch a caller-supplied locator during
contribution.

Required behavior:

- Preserve `source_ids` as document-to-document derivation. Add explicit source
  associations rather than repurposing the field or `related_ids`.
- Support multiple sources per document. Treat authority and independence as
  attributed assessments, with unknown as a valid state.
- Record actual observations only. Importing a URL does not create a successful
  retrieval date, verified hash, or accepted evidence record.
- Preserve unknown metadata during backfill. Offer dry-run reports and reviewed,
  idempotent migration; never infer provenance from model-generated prose.
- Represent review due dates separately from observation times. Distinguish an
  immutable historical fact from a claim about present applicability. An old capture
  can support historical claims without proving current behavior.
- Exact URL duplicates, identical captures, and known shared upstream origins must
  not count as independent corroboration. Different domains do not prove independence.
- Source metadata, captured content, and joins obey the same audience policy as the
  documents through which they are exposed; shared records must not reveal private
  Human associations. Revoked access must not be bypassed through a citation.

Decisions to settle in the ADR: records in separate tables versus a smaller typed
document attachment; stable source versus observation identity; review authority;
retention/deletion; and capture storage. Separate tables improve reuse and reverse
queries but add joins, lifecycle rules, and migration cost. Typed attachments are
smaller initially but duplicate shared source metadata.

Capturing full payloads is optional and requires a storage/retention design; do not
introduce blob infrastructure by implication. A URL-only record must clearly state
that no immutable copy exists. Capture workers or clients must preserve allowed
egress boundaries, and any server HTTP I/O must be async, bounded, and outside an
open database transaction. Local blocking extraction requires offloading.

Acceptance: two observations of one changing source remain distinguishable; multiple
documents can cite a shared observation; retries do not duplicate observations;
legacy URL-only records stay unverified; private joins remain hidden; export/import
preserves identity and missing evidence honestly. Requires an Alembic revision,
schema-drift/migration tests, API/MCP contracts, extraction-manifest updates, and
the corresponding skill release. No new dependency is preapproved.

## D2. Claims tied to exact evidence

Trigger: document-level attribution repeatedly leaves ambiguity about which source
supports which assertion, or contradiction/review work cannot be represented clearly
with note text and current amendment workflows.

Depends on D1 or an accepted equivalent for durable evidence identity.
Candidate records: `vault_claims` and `vault_claim_evidence`.

A claim carries document ID, observed revision, explicit claim text, a verified
locator or text span/hash, assessment, rationale, and review attribution. Evidence
links name a source observation or a revision-qualified vault document, with relation
`supports`, `contradicts`, or `context`, and a precise passage locator where available.
Existing document derivation arrays remain authoritative for their existing purpose;
these links describe a different relationship.

Requirements:

- Verify that a claimed passage exists in the referenced content. An existing
  heading alone cannot establish that the claim or quote occurs beneath it.
- Use stable block IDs where available; otherwise an exact span plus content hash.
  Ambiguous or missing spans are unresolved, never silently rebound by similarity.
- Preserve the revision or observation reviewed. Relevant content changes require
  reassessment; unrelated metadata edits must not be represented as factual changes.
  Current `content_revision` may invalidate conservatively; exact-content hashes can
  later avoid unnecessary reassessment. Document both semantics.
- Distinguish model-proposed assessment from human acceptance. Reuse governed review
  patterns where appropriate without extending existing scopes implicitly.
- Candidate assessments: provisional, supported, contested, unsupported, superseded.
  Final names and transitions need the ADR. Confidence labels require an attributed
  rationale; they are not calibrated probabilities or retrieval scores.
- Preserve contradictory evidence and any explicit adjudication. Never count wiki
  synthesis plus its source note as two independent sources.
- Make evidence sufficiency depend on the claim and domain. A blanket two-source
  rule would wrongly reject a repository's own definitive API contract, while two
  derivative articles may prove little. Risk policy and exceptions need explicit design.

Open choice: separate claims versus attaching evidence to atomic notes. Atomic notes
avoid double authoring for one-claim content; separate claims support multi-claim wiki
pages and finer review but need synchronization when prose changes. Pilot both shapes
on real evaluation failures before choosing.

Acceptance: changed or removed passages cannot retain silently current support;
invented quotations are refused; contradictions survive; acceptance has attributable
authority; a synthetic statement cannot establish independent corroboration; private
evidence cannot be disclosed through public claims. Include semantic review tests:
matching text still does not prove entailment. Requires an Alembic revision if these
records are persisted, an ADR, reviewed API shapes, and skill/transport agreement.

## D3. Bounded batch evidence fetch

Trigger: immediate-phase transcripts show repeated independent fetches materially
dominate latency or prevent adequate evidence inspection within practical budgets.
Measure call count, latency, and response bytes before choosing a batch limit.

Existing REST `POST /notes/edges` returns ID/title/slug briefs. Reuse its policy
lessons, but do not grow that display endpoint into full document retrieval.
The proposed capability fetches explicitly enumerated document IDs with content
through HTTP and MCP using one shared service and response builder.

Requirements: bounded unique ID count and total response bytes; same per-document
read checks as individual fetch; revision-qualified results; neutral unavailable
entries or omissions that cannot distinguish nonexistent from unreadable; no automatic
neighbor expansion. Define how response budgeting reports readable content omitted
for size without confusing that with unavailable IDs. Agree quota accounting based
on bounded work; do not assume the existing brief-lookup bucket prices body retrieval.

Acceptance: singleton equivalence to normal fetch, policy parity for mixed IDs,
deterministic duplicate handling, oversized bodies, stable partial-result semantics,
and MCP budgets tested. No migration expected unless quota/scope design requires one.
Record the concrete API and quota choice in an amendment to ADR 0025 or a new ADR.

## D4. Evidence health and reassessment

Trigger: D1/D2 have actual users and stale, missing, or contradicted evidence needs
systematic attention beyond individual answers.

Provide a read-only health report for unavailable sources, due reviews, changed
source observations, affected claims, and dependent syntheses. Derive review-due
state from dates and versions; do not maintain a second boolean that can disagree.
Source change is a reassessment signal, not proof the old conclusion is false.

Offer reviewable proposals and reuse existing compilation planning. Coordinate with
the [librarian plan](librarian-plan.md), whose proposed workflow must not be assumed
implemented. Avoid building a competing review queue or scheduler. Initial operation
can be operator-invoked; background refresh is a separate infrastructure decision.

Acceptance: changing a source identifies affected evidence without rewriting prose;
reports expose no private relationships; user decisions and unresolved conflicts
remain visible. Migration needs depend on D1/D2 indexes and workflow persistence.

## D5. Retrieval quality experiments and client evaluation automation

Trigger: labelled queries show evidence-selection misses, or repeated manual client
runs justify a runner. Separate retrieval failures from synthesis/citation failures.

Maintain representative exact-identifier, paraphrase, narrow-section, conflicting,
and unanswerable queries. Measure recall at a fixed candidate budget, fetched bytes,
latency, and downstream answer support. Do not interpret RRF scores as confidence.

Candidate experiments: selective section embeddings, contextual prefixes, reranking,
and automated cross-client runs. Compare each against the same fixed baseline and
report per-case regressions. Generated prefixes are retrieval aids, never evidence.
Provider selection, cost, data egress, and any dependencies need explicit decisions.

[ADR 0034](adr/0034-embeddings-are-document-level-and-chunking-is-deferred.md)
already defines chunking triggers and the measurement script; rerun them rather
than treating its historical corpus counts as current. Preserve document-level
dedup vectors. Chunking would require a separate versioned schema and Alembic revision.

An automated model judge is advisory until calibrated against human labels. Record
judge disagreement and false acceptance; a fluent explanation is not proof of support.
A deterministic fixture test cannot stand in for an executed agent run.

## Features without a current implementation case

Server-generated answers, generalized graph traversal, replacement of Postgres
retrieval with a local file index, universal chunking, and unattended external-source
crawling are not selected by this roadmap. Reconsider only against a concrete use
case and its operational cost. Preserve the established async boundaries and
Human/Agent ownership throughout every deferred feature.
