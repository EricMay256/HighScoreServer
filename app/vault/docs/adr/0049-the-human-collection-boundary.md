# 0049. The Human collection boundary: ownership, revisions, and a change feed

Date: 2026-09-12

## Status

Accepted. Refines [ADR 0048](0048-human-vault-database-authority-and-obsidian-sync.md)'s
grant and deletion contracts; everything else in 0048 stands. Phase B1 of the
[handoff](../human-vault-handoff.md): schema and boundary only. No Human endpoint,
audience-aware read, or enrollment exists because of this decision.

## Context

ADR 0048 makes enrolled Human notes database-authoritative and requires that Agent
and Human writers can never mutate each other's content. Before any Human row can
exist, the service needs a way to know which writer owns a row, a revision that a
sync client can compare against, history that outlives a deletion, and a feed
ordered well enough to resume from.

Three facts in the existing code shape the answers.

**Ownership is implied today, and not enforced.** Every Agent write path loads its
target with `get_by_id(readable_only=True)`, which applies ADR 0014's path policy
and nothing more. `Human/03 Projects/` is `ai_read: allowed`, so a readable Human
row would be overwritable by any `vault:update` holder. The hole is latent — every
row in the corpus is under `Agent/` — and it has to close before a Human row lands,
not after.

**`content_revision` already has a contract.** Amendment proposals compare against
it, and it deliberately moves on content alone: review, promotion, and compile
declines leave it untouched (ADR 0028). The sync specification needs a token that
moves on rename, move, and deletion too. Widening `content_revision` would make
every pending proposal go stale whenever its note was promoted.

**Mutual exclusion of grants exists only as a CLI check.** `grant-oauth` refuses to
give `vault:review` to anything but a `vault:read` family, but refresh, static
`grant`, and request validation never re-check it. The specification asks for the
Human/Agent rule at all four, and says scope filtering alone is insufficient.

## Decision

**Ownership is a persisted column, tied to the path by a CHECK.**
`vault_documents.collection` is `agent` or `human`, and
`vault_documents_collection_matches_path` requires `agent` rows under `Agent/` and
`human` rows under `Human/`. A plain column rather than one generated from the path,
because a generated column would follow a move and quietly change ownership; with a
stored one, a move across trees makes the pair disagree and the database refuses it.
`vault_documents_human_has_no_agent_workflow` keeps compilation, promotion, and
compile-decline state off Human rows. The migration refuses to run if any row sits
outside `Agent/`, because classifying such a row either way — as Agent-editable or
as an unpreviewed enrollment — would be a decision nobody made.

**The mutation guard lives in the repository predicate.** Every document mutation
takes `collection`, defaulting to `agent`, and puts it in the `WHERE` clause, so the
check is atomic with the write and a wrong-collection target simply matches no row —
which every caller already renders as not found. Agent by default because every
existing caller is an Agent path, and the failure is in the safe direction: a future
Human path that forgets to say so cannot reach its own rows, and says so in its first
test. Service write paths also filter the load, so a refused target costs no
embedding call. Raw updates in the repair scripts get the same predicate.

**A second revision, `resource_revision`, moves on every readable change.** It
advances on every write to a column in the domain projection — content, path, status,
provenance — and `vault_documents_resource_revision_covers_content` holds it at or
above `content_revision`, so a path that bumps content and forgets the resource
revision fails at the database. Existing rows start at their `content_revision`.
Compile declines do not move it; that column is not in the projection.

**Three Human scopes, all operator entitlements.** `vault:human-read`,
`vault:human-write` (create, edit, rename, move), and `vault:human-delete`. 0048 named
separate create, edit, and move capabilities; no client holds a strict subset of the
three, so they could be neither usefully granted nor usefully withheld — the test
`VaultScope` states. Delete stays separate because the Obsidian client is issued
without it and the browser with it. None is baseline: `vault:human-read` reaches
notes `ai_read` withholds from agents.

**No credential holds a Human scope and an Agent mutation scope.** The Agent side is
`vault:write`, `vault:propose`, `vault:update`, `vault:delete`, `vault:review`, and
`vault:compile`. The Human side is all three Human scopes, `vault:human-read`
included: a credential that could read private Human notes and write agent-readable
ones is a channel for copying the first into the second, whether or not it can write
Human content. Enforced by CHECKs on credentials, refresh tokens, and grants (over the
union of consented and entitled scopes, which is what rotation projects); explained by
`issue`, `grant`, and `grant-oauth`; and re-checked per request by `authorize`, which
refuses a credential carrying the combination outright.

**Deletion is recoverable removal.** Selected by the user on 2026-09-12 over erasure.
`vault_human_revisions` stores a full snapshot of every accepted revision, with the
actor from the credential, and has no foreign key to `vault_documents`: by the audit
log's rule (ADR 0002), and so the history outlives a deleted note and an operator can
restore it. Snapshots rather than diffs, because the notes are small and a restore
must not depend on a chain being intact. Erasure is not provided.

**The feed is its own table, ordered by holding the corpus lock.**
`vault_human_changes` records one `upsert` or `delete` per accepted Human change, and
its `GENERATED ALWAYS` identity is the feed position. Separate from the revisions
because a tombstone must outlive content, and a feed reader wants positions, not
bodies. An identity is allocated at insert rather than commit, so it is monotonic in
commit order only if writers are serialized; every writer must hold
`CORPUS_LOCK_KEY` — already taken by every governed write — while inserting, and the
Human write path (phase B3) will assert the lock is held rather than trust it.

The migration widens constraints and grants nothing, per migration 0007's rule.

## Consequences

Every existing credential starts without the Human scopes; the feature ships off
until an operator grants them. A Human OAuth family must be authorized requesting
`vault:read` alone, like a reviewer family, because a family that consented to the
baseline's `vault:write` or `vault:propose` cannot be widened into a Human one. The
browse console's `vault:read vault:propose` family therefore cannot also author Human
notes: phase C needs its own family, as 0048 already expected.

A production row outside `Agent/` aborts the release. None is expected — the last
recorded census is Agent-only — but it has not been re-verified against production.

The path CHECK also means no row can ever live outside `Agent/` and `Human/`.
`read_policy` still lists `00 Governance/` as agent-readable, for a governance import
that has never been built; if one is, it needs its own collection value and a
migration rather than rows that belong to nobody. This narrows ADR 0010's shape-only
stance at the top level alone — which sub-folders exist remains `folders.yml`'s
business. Tests that seeded `Human/` rows as though they were unowned replicas now
say which collection they belong to.

Agent lifecycle writes now advance `resource_revision` even though no Agent client
reads it. The alternative, a column that is accurate for Human rows and stale for
every other, would be a contract that holds only where someone remembered.

A refused cross-collection write reads as not found. That is correct while no Human
row is readable by agents. Once phase B2 lets agents read `ai_read`-allowed Human
notes, an agent will see a note it cannot update, and the refusal may want a clearer
error than 404.

Not decided here: audience-aware reads, which disclosure surfaces filter Human rows,
the Human REST contract, and the lock assertion itself. Those are phases B2–B4. No MCP
tool is planned for Human writes: an MCP client is an agent client, which is exactly
what the Human grant excludes.
