# 11. `doc_status` carries the Status Map; `status` stays the visibility gate

Date: 2026-07-29

## Status

Accepted

## Context

`vault_documents.status` is `document_status_enum('active', 'flagged', 'archived')`. ADR 0008
built the read surface on it: search returns `active`, fetch-by-ID also resolves `archived`, and
`flagged` is withheld because the write path declined to endorse it.

`types.yml` gives each type its own `statuses` list, and they do not line up:

| Type | `types.yml` statuses |
| ---- | -------------------- |
| `Agent Note` | Active, Flagged |
| `Wiki Page` | **Current, Stub** |
| `Concept` | Seed, Draft, Evergreen, Archived |
| `Decision` | Proposed, Accepted, Superceded |
| `Project` | Planned, Active, Blocked, Complete, Inactive, Cancelled |

`Agent Note` happens to map cleanly, which is why this went unnoticed: the corpus in hand is
mostly agent notes, and for them `status` looks like the Status Map. It is not. A `Wiki Page` is
`Current` or `Stub`, and neither value exists in the enum. `archived` exists in the enum and in no
type's list. `Status` is a universal property in `global.yml`, so a wiki page whose status the
database cannot store is a wiki page the projector cannot re-emit as valid frontmatter.

This is the same defect ADR 0009 corrected for `kind` and `doc_type`, found in the same place and
missed for the same reason: one column was quietly serving two vocabularies because the corpus had
not yet contained an example that separated them.

Widening `document_status_enum` to the union of every type's statuses is not available. It would
be roughly twenty values, `status` would no longer mean anything on its own, and every one of them
would have to be classified as visible-or-withheld — a question `types.yml` is not answering. It
would also put ADR 0008's rule, which is a security-shaped decision about what an agent may be
handed, at the mercy of a governance file that evolves without a migration. Those two things must
not be the same column.

## Decision

**`status` stays exactly what it is. A separate nullable `doc_status TEXT` carries the Status Map
value.**

- `status` is the **vault's own visibility state** — the thing `routes.READABLE_STATUSES` gates
  on. It is a closed enum precisely because a deployment must not be able to invent a new
  visibility.
- `doc_status` is the **document's governance lifecycle** — `Evergreen`, `Stub`, `Proposed`. It is
  free text with a shape-only CHECK, validated against the owning type's `statuses` list in
  application code, for the reason ADR 0009 gives: `types.yml` is meant to evolve without a
  migration.

The two are independent by design. A document can be `status = 'active'` and `doc_status = 'Stub'`
at once — visible to callers, and known to be a stub. Nothing derives one from the other, and a
test pins that.

Nullable, because rows written before this migration have no Status Map value and nothing can
discover what it should have been. As with `doc_type`, this is about history and does not license
the importer to omit it.

## Consequences

Migration `0003_vault_path_doc_status` adds the column alongside `vault_path`. Two decisions in
one revision is a deliberate exception: both are corrections of the same shape found in the same
reading of the governance schemas, both are additive `TEXT` columns with shape-only CHECKs, and
splitting them would mean two migrations landing minutes apart with no reviewable difference in
risk.

`doc_status` is on the read surface next to `status`. Their descriptions say which is which,
because two fields both called some kind of status is exactly the pair a caller will confuse.

**The vocabulary check is not built**, same as ADR 0009. It is stricter here than for `doc_type`,
because the legal set depends on the document's own type — a `Decision` may be `Proposed` but a
`Wiki Page` may not — so validation has to consider `doc_type` and `doc_status` together, and
belongs with the contribution validation ported under ADR 0004.

ADR 0008 is unaffected. It is worth stating plainly since this ADR touches the word "status":
nothing about which documents the read surface serves has changed, and `doc_status` has no bearing
on it. A future proposal to gate reads on `doc_status` would be a new decision and should be
resisted — it would move a security boundary into a file that changes without review.
