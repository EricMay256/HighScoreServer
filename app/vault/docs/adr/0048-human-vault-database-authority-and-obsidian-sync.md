# 0048. Human notes are database-owned and Obsidian is an OAuth sync client

Date: 2026-09-11

## Status

Accepted direction, implementation pending. Amended 2026-09-12: the Obsidian
extension is optional rather than a committed deliverable, and if it is not built
a one-way Human projection takes its place (see "Amendment: the extension is
optional" below). Supersedes ADR 0047's Human ownership and importer choice and resolves ADR 0041's Human-authoring deferral. Partially
supersedes ADR 0022 for enrolled Human paths and ADR 0014's exclusion-at-import
rule for those paths. Preserves Agent write ownership, agent-facing `ai_read`
enforcement, and daily Human embedding refresh with retained older vectors.

## Context

The operator prefers the browser and wants Human notes editable there or through
an Obsidian extension. Conflicts should be uncommon but must not lose work.
Database authority serializes accepted revisions while local Markdown can hold
pending offline edits. Server-only deletion removes local deletion as a remote
mutation but still requires propagation and protection against resurrection.

The separation requested is between writers, not readers: agents may read Human
information allowed by the existing `ai_read` policy, but cannot mutate it.
Human tools cannot mutate Agent content. A folder's AI-read restriction must not
stop its Human operator from reading or editing an enrolled note. The old
importer excluded that content from storage and cannot implement this arrangement.

## Decision

Adopt the database as authority for explicitly enrolled Human notes. Build the
Human browser authoring path and a configurable Obsidian extension in this
repository. The extension uses compatible-deployment URL discovery and the same
OAuth/PKCE and family-scoped operator entitlement machinery as other clients.
(The 2026-09-12 amendment below makes the extension conditional; the browser path
is not.)

A Human vault operator role grants Human read/create/edit/move capabilities;
it grants no Agent mutations or Human deletion. A separately authorized Human
browser/operator family may receive Human deletion. Enforce mutual exclusion of
Human and Agent mutation roles, and collection boundaries at every service write
path. Client names and UI controls are not authorization boundaries.

Keep `ai_read` fail-closed for agent retrieval. Permit Human-granted reads of
enrolled Human content independently. Enrollment is not AI publication; a move
or frontmatter edit cannot silently widen AI access. Broader Human storage needs
audience checks on all disclosure surfaces, not only the browser.

Use stable Markdown identities, revision-checked uploads, guarded downloads, a
durable change feed, and retained conflict versions. Local deletion never deletes
the server note; server tombstones propagate while preserving dirty local copies
in recovery storage. No automatic conflict merging is required initially.
Preserve daily full-input Human embeddings; keep old compatible vectors until
replacement and hydrate current authorized text. Summary-only indexing is rejected.

The [active sync specification](../human-vault-sync-spec.md) defines the contracts.
Use portable plugin APIs, verify desktop first, and claim mobile support only
after actual platform tests. Source and installation instructions live in
`clients/obsidian/`; credentials and real corpus content do not.

## Amendment: the extension is optional (2026-09-12)

The extension is not a committed deliverable. Browser authoring alone answers the
product problem this ADR was written for: the operator reads and writes in the
browser and had stopped writing Markdown. Build the extension only if offline
authoring turns out to be wanted in practice, and decide that after living with
browser authoring rather than before.

Two capabilities were bundled under one deliverable, and they separate cleanly.
Editing offline and synchronizing back is the expensive half: it is the entire
bidirectional protocol above, and only the extension provides it. Keeping local
Markdown copies current is the cheap half, and a one-way Human projection --
the mirror of the existing Agent exporter -- provides it without a sync client,
a second writer, or conflict handling.

That second half is not optional in the way the first is. Under database
authority, an enrolled note's local file freezes at enrollment unless something
downloads later revisions, so backlinks, graph and local search silently decay
over exactly the notes the operator chose to enroll. If the extension is not
built, build the one-way projection instead; leaving neither is the outcome to
avoid.

Nothing else in this decision is conditional. The service contracts of phase B
stand on their own, and a durable change feed with revisions and tombstones is
what either client consumes. Deferring the extension defers a client, not the
boundary.

## Consequences

The earlier Human mark-and-sweep importer is not the path to implement for enrolled
notes. This is a larger change: Human browser editing, dedicated API capabilities,
governance-managed IDs, revisions/history, tombstones, changes/checkpoints, and
OAuth role constraints need implementation and reviewed vault migrations.

OAuth scope declaration alone provides no endpoint; the plugin must demonstrate
the entire authenticated operation and refusal paths. Private governance and
runtime policy must agree. The Human read role must never be inferred from
ordinary OAuth consent or an application claiming to be a human client.

The selected design permits infrequent explicit conflicts without a collaborative
editing framework. Obsidian being closed/offline delays local synchronization,
not browser saves or server-side daily indexing. Rollback after browser writes
requires preserving accepted revisions before restoring Markdown authority.

No production migration, private-note enrollment, plugin release, deployment,
or schedule is performed by accepting this specification.
