# 0047. Publish selected Human notes while preserving Markdown ownership

Date: 2026-09-11

## Status

Superseded by [ADR 0048](0048-human-vault-database-authority-and-obsidian-sync.md)
for Human authority, import direction, and web editing. Daily full-input embedding
refresh and retained older vectors remain selected under that decision. The
original accepted direction below is retained as history.

The user initially selected curated Human
publishing plus recurring import and unified browsing, with database-owned Human
editing deferred. Supersedes ADR 0041's blanket deferral of Human inclusion;
its unresolved web-authoring questions remain deferred. Preserves ADR 0022's
one-writer rule and ADR 0014's read boundary.

## Context

Human Markdown and service-owned Agent notes already have different authorities,
but only the latter are routinely accessed through the service browser. The
operator now uses Markdown less and regards the Obsidian vault as stale and a
disfavored write path. That interpretation correctly describes generated Agent
files but incorrectly generalizes to authoritative Human notes.

A recurring import and Agent export can make both representations useful without
a bidirectional synchronization protocol. They cannot promise that every device
is current or make the browser an authoritative Human editor. A browser editing
an imported replica would create a second writer whose work the next import
could overwrite.

## Decision

Publish an explicitly selected, policy-permitted subset of Human Markdown into
the service and make it available beside Agent content in the existing browser.
Start with an export rehearsal and a small publication pilot, then add complete
browser integration and recurring operation. Human Markdown stays authoritative;
Agent Markdown remains a generated projection of the service.

Human import is replication through a dedicated operator path, not a contribution
through the Agent dedup gate. Validate content and provenance, reconcile using
source paths/hashes, and keep each row's identity stable for ordinary edits.
Replica content and lifecycle are not writable through ordinary service mutation
or amendment workflows. Import selection narrows the existing path read policy;
it never grants additional access. No previously excluded personal content becomes
hosted merely because a human uses the browser.

Imported notes join retrieval but initially do not join Agent dedup comparisons
or automatic compilation. Human imports run continuously while online and update
browser/lexical content without an embedding call. A separate daily job embeds
each changed note's latest imported input, retaining the last successful compatible
vector until replacement. New notes are lexical-only until first indexed.
Summary-only indexing is rejected; ADR 0013's full input assembly is preserved.

This permits explicit semantic lag for Human replicas and is a bounded exception
to the general write-path consistency/dedup rule. Agent contribution behavior
remains unchanged. An old vector's input hash/time remain truthful; retrieval
hydrates current text and enforces current read/publication policy. Withdrawal
and deletion never wait for embedding refresh, and obsolete job results cannot
overwrite a newer index or resurrect content. Additional use in synthesis needs
a deliberate withdrawal/provenance design. Job recommendations and acceptance
criteria are in the [daily embedding specification](../human-embedding-refresh.md).

The browser states collection, edit destination, and last observed freshness.
Import and export have separate outcomes; a completed DB snapshot is not proof
of delivered files. No UI describes all Markdown as stale or read-only.

Detailed phase requirements and technical recommendations are in the
[handoff](../human-vault-handoff.md) and
[transfer specification](../markdown-transfer-spec.md). They do not select real
notes or grant production credentials. Exact new transport, schema, and operational
settings are reviewed within their implementation phase.

## Consequences

The selected approach reuses the browser and export service, preserves the
existing local Human workflow, and avoids a second writer. It requires a Human
reconciler, durable transfer state with Alembic revisions, server-side replica
guards, and explicit browser source/freshness contracts. Source policy and runtime
policy must be checked together across repositories.

Rename-plus-edit remains ambiguous without an explicit move mapping or durable
source ID; it must be reported rather than guessed. A schedule on an offline
machine cannot provide current replicas. Exports provide offline reading and
portability, not complete service recovery or offline service writes.

Human authoring may still suffer when editing requires leaving the browser. The
operator's existing preference is evidence to evaluate, not something better
labels are assumed to cure. [Database-owned Human authoring](../human-web-authoring-deferred.md)
is documented as the next architectural alternative and remains deferred until
the user selects it.
