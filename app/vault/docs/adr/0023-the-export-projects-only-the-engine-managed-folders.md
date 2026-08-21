# 23. The export projects only the engine-managed folders, not all of `Agent/`

Date: 2026-08-19

## Status

Proposed.

Refines ADR 0022 (two trees, one writer each), which said the exporter "projects `Agent/`
only" without saying which parts of `Agent/`. Depends on ADR 0010 (`vault_path` is the only
policy key) and ADR 0014 (`ai_read` is a rule about agents, not about humans). Implements
the direction set by `Vault/00 Governance/Promotion Policy.md` and `Schemas/folders.yml` in
the private knowledge-platform repository; where this ADR and those documents disagree,
those documents win and this one is wrong.

## Context

ADR 0022 established that `Agent/` is authoritative in the database and reaches a human by
export. Read literally, "projects `Agent/` only" means every row whose `vault_path` begins
`Agent/`. `folders.yml` classifies four folders under that root, and they are not alike:

| Folder | `engine_managed` | `ai_write` |
| ------ | ---------------- | ---------- |
| `Agent/notes/` | true | `engine_only` |
| `Agent/review/` | true | `engine_only` |
| `Agent/wiki/` | true | `engine_only` |
| `Agent/Promotion Candidates/` | **false** | `allowed` |

The Promotion Policy is explicit about the odd one out: Promotion Candidates is "a
human-curated queue, **not** an engine-managed store: the engine owns `Agent/notes/` and
`Agent/review/`... Candidates are staged for human judgment, so they are kept outside the
engine's dedup gate on purpose." `types.yml` says the same thing from the other side: the
`Agent Note` type's `folder_globs` are `Agent/notes/**` and `Agent/review/**`, and the
promotion queue is not among them.

The handoff plan for compilation assumed the opposite — that notes carrying a proposed type
would be *exported into* `Agent/Promotion Candidates/`. That would make the exporter a
second writer to the one folder under `Agent/` whose whole purpose is that a human decides
what is in it, and it would file `Agent Note`-typed files in a folder no type claims.

## Decision

**The export projects the folders `folders.yml` marks `engine_managed: true`, and no
others.** `EXPORTED_PATH_PREFIXES` in `export.py` names `Agent/notes/`, `Agent/review/`, and
`Agent/wiki/`. It is a constant rather than configuration, for the reason `read_policy.py`
gives about `READABLE_PATH_PREFIXES`: which folders a machine may write into is a governance
decision, and a deployment must not be able to opt into projecting over a folder the
governance layer reserved for a person.

A row whose `vault_path` falls outside those prefixes is refused, not skipped. The same
guard runs where a path becomes a filesystem write, because an export that escapes its
output directory is the one failure re-running cannot undo.

**Pruning is scoped to the same prefixes.** A retired document (ADR 0019 deletes rather than
tombstones) has to leave the tree, so the projection deletes markdown files under those
folders that no row accounts for. Files outside them — `Agent/INDEX.md`, anything a human
staged in the promotion queue — are never deletion candidates, whatever the corpus holds.

**A note is marked for promotion by a field, never by a path.** Whatever surfaces the
promotion queue reads `proposed_doc_type` and reports it. It does not move, copy, or write
the note into `Agent/Promotion Candidates/`. Promotion stays what the Promotion Policy says
it is: a human rewriting the note into `Human/`, "not a copied agent note".

## Consequences

### The privilege argument from ADR 0022 survives intact

ADR 0022 made `proposed_doc_type` a hint precisely so no contributor input could reach
`vault_path`. Had the exporter instead routed proposed-type notes into a different folder,
contributor input would have chosen a destination path after all — one folder further along,
but by the same mechanism. Keeping the queue out of the projection closes that loop rather
than relocating it.

### The promotion queue needs a surface that is not a folder

Reporting a pending queue now means a read over `proposed_doc_type`, gated by `vault:review`,
rather than a directory listing. That is more work than writing files, and it is the price of
leaving the folder to its curator. It also removes a whole class of question — what happens
when a human edits or deletes a file the exporter believes it owns — because the exporter
never owned it.

### `Agent/` is not one thing, and the code now says so

`EXPORTED_PATH_PREFIXES` and `READABLE_PATH_PREFIXES` are deliberately different tuples:
the first is derived from `engine_managed`, the second from `ai_read`, and
`Agent/Promotion Candidates/` is in one and not the other. Anyone tempted to collapse them
into a shared "the Agent tree" constant is collapsing two governance fields that disagree.

### What this does not decide

Whether the promotion queue's files are ever *generated* at all. A human may want the
exporter to draft a candidate for them to edit. That would be a new decision about who owns
that folder, made in the governance repository first, and it would supersede this ADR rather
than amend it — engine-managed or human-curated is the whole of it.
