# 0051. Human writes are revision-checked, recorded in one transaction, and retry-safe

Date: 2026-09-12

## Status

Accepted. Phase B3 of the [handoff](../human-vault-handoff.md). Builds on
[ADR 0049](0049-the-human-collection-boundary.md)'s revisions, history and feed,
and [ADR 0050](0050-human-reads-the-audience-is-the-collection.md)'s audiences.
Deletion is phase B4 and is not decided here.

## Context

A Human note is edited in two places that are not always connected: the browser,
which is authoritative the moment a save is accepted, and a sync client, which may
hold edits made offline. The specification asks that local files never overwrite
unseen server revisions, that a create be safe to retry, and that a response lost
after a server commit be survivable. It also says a move must not silently change
who may read a note.

Two properties of the existing service apply. Every governed write takes one
corpus-wide advisory lock, and ADR 0049 made the feed's ordering depend on it. And
the contribution path already has an idempotency ledger keyed by principal and
key, with a digest rule every stored row names.

## Decision

**Three routes under `vault:human-write`.** `POST /human/notes` creates,
`PUT /human/notes/{id}` replaces authored content, and
`POST /human/notes/{id}/move` renames or moves. Deletion is not among them.

**Each accepted write is one transaction under the corpus lock** that changes the
row, writes a revision snapshot and a feed entry, and appends an audit event. No
observer can see a change without its history, or history without its change.
`VaultHumanHistoryRepository.record` queries `pg_locks` and refuses to run without
the lock, rather than trusting its callers: a writer that skipped it could commit
a feed position a reader has already passed, and nothing downstream could tell.

**No embedding call and no dedup gate.** A save is keyword-searchable on commit and
semantically indexed by the daily job. Duplicate detection compares agent notes
with agent notes only (ADR 0050, confirmed by the user 2026-09-12), so a Human
write neither runs the gate nor feeds it.

**Create takes an operation id, through the existing write ledger.** The author
chooses the path, and the service mints the id. A resend with the same id and
body returns the note it made, with 200 rather than 201; the same id with a
different body is a 409. The ledger is shared with contributions and one OAuth
client may hold an Agent family and a Human family under one principal, so a
replay also requires that the prior entry produced a Human note. The digest rule
is `canonical_request_digest`'s, widened to any request model: one ledger, one
rule. A replay whose note has since been removed is a 404 and does not recreate
it.

**Edit and move are retry-safe without a key.** A request the note already
matches succeeds without writing, whatever base it names. Only a request that
would change a note whose `resource_revision` differs from the base is a
conflict: 409, with `current_resource_revision` in the body so the client knows
which version to fetch and resolve against. A client resending after a lost
response therefore gets its own result back rather than a conflict with itself.
A body field and 409 rather than `If-Match` and 412, because that is how this
service's amendment proposals already carry `base_revision`; the HTTP-native form
was considered and would be a fair alternative on a greenfield surface.

**An edit replaces authored content, including authored metadata.** A person's
governance type, status and unmodelled frontmatter live in the file they edit, so
the edit carries them, where an agent update leaves them to the service. Both
revisions and `updated_at` move. A title change never moves the path.

**A move changes the path and nothing else.** `resource_revision` advances;
`content_revision` and `updated_at` do not, the reading promotion already gives a
move. It is refused with 422 if it would change whether agents may read the note —
widening or narrowing that is a governance change to a folder, not a side effect
of a rename (ADR 0048) — and with 409 if another note has the path ignoring case.

**The path rule.** A Human path starts with `Human/`, ends in `.md`, and has no
empty segment, no segment beginning with a dot, no whitespace at either end of a
segment, no backslash, and no control character. Stated once in
`domain.human_path_problem` and applied by the request models and the service, so
a shape the database would refuse arrives as a 422 rather than a 500. Case is
compared insensitively for collisions, because two paths that differ only in case
are one file on Windows and on a default macOS volume.

**The Human read model returns `frontmatter`.** A sync client writes whole files
back, so it needs what it sent. The agent read model is unchanged.

## Consequences

A sync client resolves a conflict by fetching the current note and writing again
against its revision. The service does not merge, as the specification allows.

There is no route that moves a note across an `ai_read` boundary. The governance
workflow the refusal points to — changing a folder's policy in the private
`folders.yml` and `read_policy.py` together — exists, but no in-service workflow
does. A person who wants a note in a differently governed folder moves it outside
the service for now.

A create into a folder `ai_read` allows is readable by agents at once. Placement is
the author's decision, and the browser (phase C) should say so when it is made.

Operation ids share the ledger's namespace per principal with contribution keys.
Collisions are refused, never silently resolved, and the content-derived shape of
contribution keys makes one unlikely.

The case-insensitive path check scans `lower(vault_path)`. That is nothing at a
corpus in the hundreds and wants an expression index before tens of thousands.

Every Human write, including a no-op resend, appends an audit event, for the
reason contribution replays do: a retry must not disappear from reconstruction.
