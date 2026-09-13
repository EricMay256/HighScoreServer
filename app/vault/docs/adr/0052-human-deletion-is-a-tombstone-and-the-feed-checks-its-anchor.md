# 0052. Human deletion leaves a tombstone, and the feed checks the entry it resumes from

Date: 2026-09-12

## Status

Accepted. Phase B4 of the [handoff](../human-vault-handoff.md), and the last slice
of the Human service boundary before the disclosure regression suite. Builds on
[ADR 0049](0049-the-human-collection-boundary.md)'s history and feed tables and
[ADR 0051](0051-human-writes-are-revision-checked-and-retry-safe.md)'s write
semantics.

## Context

The user chose recoverable removal over erasure on 2026-09-12. The specification
asks that server deletion propagate to clients that were offline when it happened,
that nothing — a retried create, an upload of dirty local edits — bring a deleted
note back, and that a client learn of every change without the feed ever silently
skipping one, including after the database behind it is restored to an earlier
point.

ADR 0049 already made the feed's positions monotonic in commit order, by requiring
the corpus lock of every writer. What remained was deletion itself, a way to undo
it, and the read side a sync client pulls from.

## Decision

**Deletion removes the row and records a tombstone.**
`DELETE /human/notes/{id}?base_resource_revision=N`, under `vault:human-delete`, is
one transaction under the corpus lock: a `delete` snapshot of the note's last
content and a `delete` feed entry at revision N+1, then the row's removal and an
audit event. A stale base is a 409 carrying the current revision, since a note that
changed since the caller looked should be looked at again before it goes. The
revision is a query parameter because intermediaries may drop a DELETE body.

**A resent delete returns the same tombstone.** A note already deleted answers 200
with its latest `delete` entry, for the reason a resent edit succeeds: the request
is already true. An id that was never a Human note is a 404.

**Only restore brings a note back.** Once the row is gone, an edit or move finds
nothing (404), and a resent create finds its ledger entry detached from any
document (404) — the same refusal B3 already gave. Restore is an operator action —
`VaultHumanNoteService.restore`, driven by `scripts/restore_human_note.py` — and not
a route: undoing a person's deletion is rare, and a scope for it would be one more
grant to issue carefully for no user who needs it. It recreates the row **under its
original id**, from the latest snapshot, so every client that knew the note knows
it again. Its revisions continue past the tombstone, so a client that applied the
deletion sees the restore as newer; `created_at` and the author come from the first
snapshot, so the note keeps the day it was written. It is refused when the note is
live or when another note has taken its path.

**The feed is two routes under `vault:human-read`.** `GET /human/changes?after=…`
returns entries past a cursor, oldest first, each with its own cursor so a client
can checkpoint after applying any one of them. `GET /human/changes/head` returns
the cursor of the newest entry. Every change is delivered, with no compaction: a
client decides what to fetch by comparing each entry's revision with its own.

**A cursor names an entry, and the feed checks the entry is still there.** The
cursor is `cursors.py`'s opaque token with its own walk name, carrying the
position, the note id and the revision. On resume the feed loads the entry at that
position and compares all three. If it is missing or different, the database is
not the one that issued the cursor — restored to an earlier point, where new
changes reuse old positions, or replaced — and the answer is **410 Gone: take a new
snapshot**. No epoch table is involved, and none would work: a stored epoch is
restored along with everything else, and a check against the entry itself is not.
The same check will cover pruning, if the feed is ever pruned.

**There is no snapshot endpoint.** A snapshot is: read the head, page the existing
`/human/notes` listing, then replay the feed from the head. A change that commits
while the listing is paged has a position past the head, so the replay delivers it;
a note that moved across a page boundary is caught the same way. Applying an entry
twice is harmless, because every entry carries its revision. That is consistency
without holding a transaction open across requests.

## Consequences

Tombstones and snapshots accumulate without bound. Nothing prunes either table,
which is what recoverable removal and a gap-free feed both cost. If pruning is
added, the anchor check turns a client behind the pruning floor into a 410 rather
than a silent gap.

A client behind a 410 resnapshots, and must not read a note's absence from the new
snapshot as permission to recreate it: the specification says so, and the server
will not accept an edit to a deleted id anyway.

A restored note keeps its id and its path. When the path has been taken, the
operator moves or renames the new note first; restore does not choose a path of
its own, because a restore that quietly relocated a note would be a second decision
nobody made.

A deleted note's embeddings are removed with its row. After a restore, the daily
Human embedding job finds it unindexed and indexes it; until then it is findable by
keyword only.

The feed discloses the paths of deleted notes to `vault:human-read` holders. They
already read those notes while they existed; the agent surface sees neither.
