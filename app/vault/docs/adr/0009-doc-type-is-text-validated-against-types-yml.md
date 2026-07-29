# 9. `doc_type` is text validated against `types.yml`, not a second enum

Date: 2026-07-29

## Status

Accepted

## Context

`vault_documents.kind` is `document_kind_enum('note', 'wiki')`. `types.yml` defines sixteen
types — Daily, Project, Area, MoC, Note, Decision, Reference, Person, Resource, System, Meeting,
Concept, Summary Note, Idea, Agent Note, and Wiki Page — and the corpus cannot be imported
without somewhere to put the type.

`Type` is a **required universal property** in `global.yml`. The projector's job is to render the
database back into Markdown that the validator accepts, so a type the database does not store is
a type the export cannot emit. That is the load-bearing reason for this column, and it holds
regardless of which layers ever reach the database.

The tempting move is to widen `kind`. It does not survive contact with what `kind` already does:

- `kind` is **load-bearing in a constraint**. `vault_documents_compile_provenance_consistent`
  reads `kind = 'note'` versus `kind = 'wiki'` to require that compiled documents carry
  `compile_run_id`, `compiled_by`, and `compiled_at`, and that authored ones carry none of them.
  Folding the other fourteen `types.yml` values into that enum means every one of them has to be
  classified as authored-or-compiled inside a CHECK constraint, which is a lifecycle question
  `types.yml` is not answering.
- The two columns **change for different reasons**. `note` versus `wiki` is a fact about how a row
  got here and what may write it. "Decision" versus "Meeting" is a fact about what the content is.
  Nothing suggests they will move together.

### How much `doc_type` actually adds, stated honestly

This ADR was first written before `types.yml` and `folders.yml` were available, and one of its
arguments was weaker than it looked. For the Agent layer — the corpus the importer is actually
scoped to — `kind` and `doc_type` are close to isomorphic:

| Folder | `allowed_types` | `kind` |
| ------ | --------------- | ------ |
| `Agent/notes/**`, `Agent/review/**` | `Agent Note` | `note` |
| `Agent/wiki/**` | `Wiki Page`, `MoC` | `wiki` |

So on the corpus in hand, `doc_type` adds exactly one bit: `Wiki Page` versus `MoC`. Anyone
reading the "they change for different reasons" argument alone would over-estimate the column.

Three things keep it justified anyway, in descending order of strength:

1. **The projector needs `Type` to round-trip.** It is required frontmatter. `kind` cannot
   reconstruct it, because it cannot separate `Wiki Page` from `MoC`.
2. **`MoC` and `Note` are universal types** — `folder_globs: ["**"]` in `types.yml` — so they are
   permitted in any folder, including the Agent layer, and are exempt from a folder's
   `allowed_types`. A pure-Agent corpus can therefore already carry types that `kind` does not
   encode.
3. Whether Human-layer material ever reaches the database is **open**, not settled — see
   deferred decision 1. `Agent/Promotion Candidates/**` and `Human/01 Inbox/AI/**` are both
   `ai_write: allowed`, so they are the plausible places where an agent-written document carries
   a human type such as `Project` or `Concept`.

Point 1 alone is sufficient. The others are why the column is unlikely to stay one bit wide.

That leaves where the vocabulary is enforced. A PostgreSQL enum requires a migration to extend.
`types.yml` is explicitly designed to evolve without one — it is the faster-moving artifact of the
two. Encoding the type vocabulary as an enum would put the slower mechanism in charge of the
faster concept, so that adding a note type to a governance file becomes an Alembic revision, a
review, and a deploy.

A lookup table with a foreign key was the third option considered. It gets database enforcement
while staying extensible by `INSERT` rather than DDL. It was not adopted **now** because it makes
`types.yml` and the table two sources of truth that must be kept in sync, and the sync step is
itself a thing that can be wrong. It stays available: adding the table and the FK later is a
purely additive migration, and this ADR does not foreclose it.

## Decision

**`kind` stays the coarse storage and lifecycle discriminator. A separate nullable
`doc_type TEXT` carries the Type Dictionary value, validated in application code against
`types.yml`.**

The database constrains **shape only** — `CHECK (doc_type IS NULL OR doc_type ~
'^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$')`. Non-blank, bounded at 64 characters, no control
characters, and an interior space is allowed because "Agent Note", "Wiki Page", and "Summary Note" are real
type names.

The split of responsibility is the point:

- The **database** enforces what is true regardless of governance version — that a stored type is
  a short, printable, non-blank string. That rule does not change when `types.yml` does.
- **Application code** enforces which names are legal. That is what keeps adding a type a data
  change rather than a migration.

A well-formed name that is in no Type Dictionary is therefore accepted by the database and
rejected at the write boundary. That is deliberate, and it is pinned by a test so nobody later
"fixes" the constraint into a vocabulary check.

**The column is nullable, and null means untyped.** Every row written before this migration has
no type, and nothing can retroactively discover what it should have been. `NOT NULL` would have
to be backfilled with an invented default, which would make "we chose this type" and "we had to
write something" indistinguishable. This is the same reasoning as ADR 0003, where absence of a
row in `vault_document_embeddings` means unembedded rather than a nullable column on the
document.

Whether the *importer* may write a null is a separate and stricter question, and this ADR does
not license it: `Type` is required frontmatter, so an imported document that reaches the database
without one has already failed validation upstream. Nullable here is about history, not about
relaxing the write path.

## Consequences

Migration `0002_document_doc_type` adds the column and the shape constraint. It is additive and
takes no table rewrite; the downgrade drops the column and is lossy, which is acceptable for a
local and test rollback only.

`doc_type` is on the read surface. `VaultDocumentDetail` carries it and reports `null` explicitly
for an untyped document rather than omitting the field, so "untyped" and "this deployment predates
`doc_type`" do not look the same to a caller. The vault's consumer is an agent choosing what to
read, and the type is exactly the kind of signal it should not have to infer from the body.

**The vocabulary check itself is not built here.** Nothing in the read-only slice writes a
document, so there is no write boundary to validate at yet, and `types.yml` lives in the private
knowledge-platform repository rather than in HSS. When the importer or the governed write path
lands, the check belongs beside the rest of the contribution validation ported under ADR 0004 —
one place that loads the governance vocabulary and rejects an unknown type before insert. Until
then, the only thing standing between a bad type and the table is the shape constraint, and that
is stated here so it is not mistaken for full enforcement.

`VaultContributionRequest` is unchanged. It describes the v1 tool contract for the unbuilt write
path, and widening a contract whose source of truth is outside this repository is not a decision
this ADR is entitled to make. Whether contributors supply a type, or the importer derives it, is
open.

Deferred decision 2 in `vault-architecture.md` is closed by this ADR. Deferred decision 1 — the
missing path and policy-scope column — is untouched and still blocks the importer.
