# 0048. Human notes are database-owned and Obsidian is an OAuth sync client

Date: 2026-09-11

## Status

Accepted direction, implementation pending. Supersedes ADR 0047's Human ownership
and importer choice and resolves ADR 0041's Human-authoring deferral. Partially
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
