# 22. Two trees, one writer each: humans author markdown, agents author through the service

Date: 2026-08-19

## Status

Accepted.

Depends on ADR 0010 (`vault_path` is the only policy key), ADR 0011 (`status` and
`doc_status` are different things), ADR 0012 (markdown layers reconcile by mark-and-sweep),
and ADR 0014 (`ai_read` excludes at import and at query). Implements the direction the
private knowledge-platform repository already set in `Vault/00 Governance/AI Contribution
Policy.md` and `Promotion Policy.md`; where this ADR and those documents disagree, those
documents win and this one is wrong.

## Context

Notes contributed through the vault service are lossier than notes written as markdown, in
three specific ways. `service.py` hardcodes `doc_type="Agent Note"` and `doc_status="Active"`
on every contribution, against sixteen types and eight statuses in `types.yml`. And there is
no export, so a note that exists only in Postgres has no file for wiki compilation to read —
`source_sha256 IS NULL` marks it as database-authored, which is exactly how mark-and-sweep
knows not to sweep it, and equally why it has no upstream file.

The intended end state is that agents reach the corpus **only** through the service, while a
librarian compiles it into a markdown Obsidian vault for human browsing.

Reconciliation today runs markdown → database. Adding an export in the other direction over
the same paths would create a round trip: an exported file is walked by the next
reconciliation, imported, gains a `source_sha256`, and thereafter looks markdown-authored —
after which deleting the file deletes the note.

## Decision

**Two trees, each with exactly one writer, and they do not overlap.**

| Tree | Authoritative in | Flow |
| ---- | ---------------- | ---- |
| `Human/` | markdown | markdown → service, by the existing importer, so agents can read it |
| `Agent/` | the service | service → markdown, by a new exporter, so humans can read it |

Because the trees are disjoint, there is no loop, no conflict resolution, and no sync
protocol. The direction of authority is a property of the path, not a convention someone has
to remember.

**Reconciliation stops scanning `Agent/`.** ADR 0012's sweep is scoped by path prefix
precisely so this is expressible without rewriting it. Until that happens the Stage A engine
and the service are two writers to one tree, which is the loop above.

**A contributing agent may set `doc_status` and may propose a type. Neither changes where the
note lives.**

- `doc_status` is the agent's own lifecycle claim — an agent that knows it is writing a stub
  says so. Validated against the Status Map for the note's actual type.
- `proposed_doc_type` is a **hint stored beside the note, never applied to `doc_type`**. It
  marks the note for human review and is reported, not enacted.
- `status` remains untouchable by contributors, per ADR 0011.

**Promotion into `Human/` stays a human rewriting the note.** `Promotion Policy.md` is
explicit that promotion "almost always involves processing... a promoted note becomes a
first-class Human note, **not a copied agent note**", and that nothing in
`Agent/Promotion Candidates/` is canonical until a human promotes it. A proposed type routes
a note into that queue; it does not move it into the human hierarchy.

**The librarian surfaces the queue twice**: in the compile run's report, and when the human is
tending the human vault. A proposal nobody is reminded of is a proposal that rots.

## Consequences

### Why the proposed type is a hint and not a destination

This is the decision the rest depends on, and the reason is a privilege boundary rather than
taste.

`vault_path` is the only policy key (ADR 0010), `folders.yml` governs `ai_write` per folder,
and the AI Contribution Policy lists the Human areas agents may **not** write to. If an
agent-supplied type selected a folder, an agent would choose its own note's path, and
therefore choose whether it landed in a tree it is forbidden to write. Storing the proposal
separately from `doc_type` removes the mechanism entirely: there is no code path from
contributor input to `vault_path`.

It also keeps `READABLE_PATH_PREFIXES` honest. That tuple is deliberately not configuration —
"a deployment must not be able to opt into serving content the governance layer excluded" —
so a type→folder map that could introduce a new destination would make a governance change
look like a data change.

### What the exporter must be

Idempotent and byte-stable. Re-running it over unchanged notes must produce identical files,
or every run is a meaningless diff and the git history stops being an audit log. That means
deterministic frontmatter ordering and stored timestamps, never `now()`.

It projects `Agent/` only. `ai_read` governs what *agents* may be served and is not a filter
on the human projection: a human browsing their own vault is not the threat model ADR 0014
addresses.

### What this costs

The Stage A `vault_contrib` write path for `Agent/` is retired, and the `knowledge-vault`
skill has to reach the service instead. That is the largest piece of this work and most of it
lives in the other repository.

Humans lose the ability to edit agent notes as text. That is the point — it is what makes the
tree single-writer — but it is a real change in how the vault is used, and it means a
correction to an agent note goes through the service's update verb rather than an editor.

### What this does not decide

Whether `Human/` content is ever migrated *into* the database as the source of truth. It is
not, under this ADR: `Human/` stays markdown-authoritative and reaches agents by import. If
that ever changes, this ADR is superseded rather than amended, because the single-writer
property is the whole of it.
