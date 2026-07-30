# 12. Markdown-authored layers reconcile by mark-and-sweep over a content hash

Date: 2026-07-29

## Status

Accepted

## Context

Human notes are authored as Markdown and always exist as Markdown first. The database is a
**replica** of `Human/**` and the **system of record** for `Agent/**`. That asymmetry creates two
problems one-way import does not solve: a file that changes leaves the row stale, and a file that
is deleted leaves the row orphaned.

The decision is constrained by a fact in `global.yml`: **`ID` is engine-owned and an error on a
canonical human note.** Checked against the corpus — 80 human notes, none carrying `ID`; 36 agent
notes, 32 carrying one. So an agent note brings its own identity and a human note brings none.

That makes identity and reconciliation the same problem, because manufacturing an id decides what
a rename does:

| id derived from | rename, no edit | edit, no rename |
| --------------- | --------------- | --------------- |
| the path | new id → delete + create | stable |
| content hash | stable | new id → delete + create |
| generated UUID | stable *if the file can be matched to the row* | stable *ditto* |

The UUID row is the one worth having, and the condition in italics is the whole difficulty:
matching a file to an existing row needs something stable, and path and content are the only
candidates — each breaking under exactly the operation the other survives.

### Change detection

`mtime` is a stat call with no file read, and it lies in both directions: a fresh clone stamps
every file with checkout time so everything looks changed, Syncthing across devices brings clock
skew, and touch-without-edit triggers a needless re-import. Its only advantage is avoiding a file
read, which at 116 notes is not an advantage. The asymmetry matters more than the cost: a false
"changed" spends money, because re-import implies re-embedding.

A **content hash** is exact, survives clone and clock skew, and makes re-import idempotent — which
the architecture doc already requires, describing import as a resumable operational job.

### Deletion

**Mark-and-sweep** walks everything, upserts by `vault_path`, and deletes rows whose path was not
seen. It is self-healing: it converges from any prior state, including a partial previous run, a
deletion that happened while the importer was off, or a manual database edit.

**A git diff** against a stored commit sha is the alternative, and the vault genuinely is a git
repository, so it is available. It detects renames natively, which is the one thing sweep cannot
do. It was not adopted because it sees only *committed* changes — uncommitted working-tree edits
are invisible, and Syncthing distributes the projection rather than the git repository, so a
device can be ahead of the last commit. It also cannot self-heal: if the database drifts, a diff
never notices, so a periodic full reconcile is needed anyway, which means building sweep regardless.

## Decision

**Mark-and-sweep, keyed on `vault_path`, with change detection by `source_sha256`. Human-note ids
are generated UUIDs. A rename is a delete plus a create, except when the sweep can prove otherwise
from the hash.**

`vault_documents.source_sha256` is the SHA-256 of the upstream file, `BYTEA` with a 32-byte CHECK.

**NULL means the row has no upstream file** — it was authored in the database. That is not a
missing value; it is the row stating which direction truth flows for it, in the same way that
absence of a row in `vault_document_embeddings` means unembedded (ADR 0003). Agent-layer rows
carry NULL permanently.

Three properties the sweep must have:

- **It is scoped by path prefix.** An unscoped sweep would delete every agent note, since none has
  a source file. Sweeping `Human/%` is a prefix query, which is what the `text_pattern_ops` index
  from ADR 0010 exists for.
- **It sweeps only after a complete walk.** A partial or interrupted run must not delete, or it
  removes everything it did not reach.
- **It refuses to sweep an implausible result.** A misconfigured vault root that resolves to an
  empty directory would otherwise empty the table. A floor on the number of files seen turns that
  into a refusal instead of data loss.

**Move recovery.** Within a single sweep, a delete and a create whose `source_sha256` are equal
are the same document moved. The row keeps its id, its `created_at`, and its embedding, and only
`vault_path` changes. This recovers most of what git's rename detection would have given, without
a commit discipline, a watcher, or a second source of truth. It misses rename-plus-edit, which is
a new document by any reasonable reading.

## Consequences

Migration `0004_reconciliation` adds the column. Nothing populates it yet — the importer is
unbuilt — so every existing row is NULL, which correctly reads as "authored here" for the agent
fixtures that are the only rows in existence.

**Human ids being generated UUIDs makes `vault_documents.id` heterogeneous**: an opaque slug from
the agent engine, or a UUID for imported human notes. The column is `TEXT` with only a non-blank
check, so this needs no schema change, but nothing may assume a format. In particular `related_ids`
and `source_ids` are text arrays with no foreign key, and a human note's id is stable only as long
as the row survives — a rename-plus-edit will break references to it. That is a real limitation of
identity-less source files and not something the database can fix.

**Deletion is destructive and `scores`-style protection does not exist here.** `vault_documents`
has no `ON DELETE RESTRICT` guarding it; a swept row takes its embeddings with it via CASCADE.
This is correct for a replica, whose content is recoverable from disk, and would be wrong for the
agent layer, which is why the prefix scoping above is a safety property rather than an
optimization.

**Re-import and re-embed are deliberately separated**, and `source_sha256` governs only the first.
See ADR 0013.
