# Human web authoring: formerly deferred design analysis

Date: 2026-09-11

Status: historical analysis. The user subsequently selected database authority,
Human browser authoring, and an OAuth Obsidian sync client with server-only
deletion. [ADR 0048](adr/0048-human-vault-database-authority-and-obsidian-sync.md)
and the [active specification](human-vault-sync-spec.md) resolve the deferral.
The comparisons and former open decisions below explain the alternatives; their
references to work remaining deferred are historical, not the current scope.

## Evidence and reason to revisit

The operator already prefers the service browser and uses Obsidian less. The
generated Agent projection has made the entire Markdown vault feel stale and
discouraged as a writing destination, even though Human Markdown is authoritative.
This meets ADR 0041's original trigger: the browser is useful and the separation
is felt. Options 1 and 2 address access; they may leave authoring friction intact.

Evaluate the selected pilot during ordinary use. Revisit this option if a Human
note is not captured or improved because doing so requires leaving the browser,
if source-location controls do not help, or if reliable imports still leave the
operator unwilling to use Markdown for writing. Another direct request for web
authoring is sufficient. Do not require an arbitrary usage threshold to dismiss
the operator's reported friction, and do not infer approval from browser preference.

## Proposed user experience

The browser supports creating, editing, moving, and retiring Human notes. A save
updates the authoritative service revision immediately; listing and lexical
retrieval reflect it, and embedding freshness is explicit. Deliberately related
Human notes save without the Agent duplicate-rejection gate. Direct Human edits
are attributed to the authenticated human and retained in revision history.

Agent suggestions remain proposals. The human's own save should not require
submitting a proposal to themselves and then changing credentials to accept it.
Preserve the separate Agent review workflow rather than granting broad destructive
tools to ordinary agent clients to make the Human editor convenient.

Markdown becomes an exported reading/portability copy for the migrated Human
collection. Existing unmigrated Human collections may remain Markdown-owned, but
ownership must be explicit per collection and every note must have exactly one
authority. A cutover changes the direction of flow for its exact selected set.

## Decisions required before implementation

| Decision | Recommended direction for evaluation | Cost or alternative |
| --- | --- | --- |
| Human editor authority | Dedicated Human authoring capability and server-enforced ownership | Extending generic Agent update scopes is smaller but grants inappropriate reach |
| Visibility | Separate human access from agent-readable publication | Current shared `vault:read` is simpler but cannot host excluded personal notes safely |
| Identity | Preserve existing imported IDs at cutover; paths remain movable locations | Reminting IDs breaks references and historical browser links |
| Concurrent editing | Revision-checked saves with a useful conflict view and recoverable versions | Last-write-wins is easier but can silently discard work |
| Offline edits | Initially explicit patches/import proposals against an exported base revision | Full offline editing/sync requires conflict and deletion semantics beyond exporting |
| Markdown compatibility | Preserve source metadata and document unsupported embeds/assets | Full Obsidian compatibility and asset hosting substantially enlarge scope |
| Human lifecycle | Deliberate retirement with recovery/history appropriate to authored content | Reusing replica sweep semantics would erase authoritative notes |

These are proposals, not settled permissions, schema choices, or selected new
dependencies. Scope and identity design must be reviewed before building routes.

## Editing through both the browser and Markdown

Two editing interfaces are possible. Two independently authoritative copies
require a synchronization protocol. Adding export to the current Human importer
does not supply one: a local edit and a browser edit can both descend from the
same earlier revision, and either direction can silently overwrite the other.

For example, both copies start at revision 10. The browser changes a paragraph
while an offline Markdown editor changes that same paragraph. On reconnection,
neither a newer filesystem timestamp nor the direction of the next job proves
which content the operator intended to keep. Even one human using two devices
can create this conflict. A deletion on one side and an edit on the other needs
an explicit resolution too, or the next import can resurrect the deleted note.

| Design | Where a browser save lands | What Markdown editing requires | Assessment |
| --- | --- | --- | --- |
| Markdown authority with queued browser edits | A durable pending edit, applied to the source by a local bridge | Existing authoring, plus base-hash validation before bridge writes | Smaller authority change, but browser save remains pending when the bridge is offline and conflicting edits need resolution |
| Database authority with managed Markdown copies | A new authoritative database revision | A sync client submits edits against the last downloaded revision and downloads accepted changes | Coherent extension of option 3; still needs conflict handling, durable file identity, and a local client |
| Independently writable DB and files with automatic synchronization | Each side accepts its own changes | Common ancestor tracking and deterministic reconciliation across both stores | Largest expansion; the present path/hash importer cannot provide these guarantees |

If editing in both places becomes a requirement, prefer evaluating database
authority with managed Markdown copies. Both interfaces can still write; the
database serializes accepted revisions while offline files represent pending
edits. This matches the operator's browser preference and gives one history for
conflict decisions. It does not make conflicts disappear or make files disposable.
The current single-writer implementation remains selected until a new decision.

Minimum additional contracts for either two-interface design:

- A stable source identity across rename-plus-edit, stored in approved frontmatter
  or a durable sidecar manifest. Current path/hash move recovery is insufficient;
  adding IDs to Human notes requires the governance decision already identified.
- The common base content/revision used by each editor. Compare base, local, and
  remote changes; accept one-sided changes, merge demonstrably non-overlapping
  text edits where safe, and surface conflicts without losing either version.
- Revision-checked writes at the service and expected-hash checks before replacing
  local files. A stale export must not overwrite unuploaded Markdown changes.
- Durable deletion records with a defined retention/reconnection policy, so an
  offline client cannot unknowingly restore a removed note. Moves, scope changes,
  and policy changes are distinct from deletion.
- Echo suppression: exported changes observed by a watcher are acknowledgements,
  not newly authored edits. Use identities and revisions/hashes, not just mtimes.
- Restartable transfer acknowledgements, conflict records, version recovery, and
  explicit UI states for saved remotely, awaiting local application/upload, and
  conflicted. A queued edit must not be presented as already applied.
- Defined fidelity for frontmatter, wikilinks, assets, and formatting, plus current
  read/write authorization on every transfer. File sync cannot widen publication.

Option 3 with read-only Markdown exports is simpler because there is one content
history and no writable offline replica to merge. Making those exports editable
adds the contracts above on top of the Human editor, permissions, and migration
work already deferred. A CRDT or collaborative-editor framework is not selected;
first establish whether explicit revision conflicts would serve actual usage.

The daily embedding job remains downstream of accepted database content in any
of these designs. It indexes the latest accepted revision at its daily cutoff;
unresolved local edits and pending browser proposals are not silently embedded
as canonical content. It cannot resolve ownership or synchronization conflicts.

## Migration and rollback requirements

1. Record a private source snapshot and the exact cutover selection, plus a
   restore-capable service backup. Compare source hashes with imported rows.
2. Resolve conflicts and link ambiguity before changing ownership. Preserve IDs,
   source provenance, note types, timestamps, and existing references.
3. Stop reconciliation for the selected set without sweeping it away. Change
   ownership transactionally; reject stale importer runs after cutover.
4. Enable Human editor writes and extend export to the newly database-owned
   collection. Protect original Markdown from accidental replacement until a
   reviewed initial projection and recovery copy exist.
5. Exercise edit, concurrent edit, rename, removal, export, and restore. Declare
   cutover complete only when both content and write ownership are verified.

Rollback after browser edits requires exporting/reconciling those edits and
reviewing divergence before re-enabling Markdown import. Restoring an old checkout
and restarting the importer is not a lossless rollback. A generated export remains
different from a database backup containing revisions, audit, and workflow state.

## Scope and ADR work

This option supersedes ADR 0022 for migrated Human paths and extends
[ADR 0047](adr/0047-human-publishing-preserves-markdown-ownership.md). It must resolve
the remaining questions in [ADR 0041](adr/0041-human-authored-notes-in-the-vault.md),
particularly human ownership, read permissions, dedup, and review. Record a new
Nygard ADR and explicit vault migrations; do not silently broaden the selected
import/browser specification into an editor project.

Attachments, simultaneous multi-device Markdown editing, private content available
only to particular humans, and Human-backed compilation each need their own
bounded acceptance criteria if included. No new infrastructure is selected here.
