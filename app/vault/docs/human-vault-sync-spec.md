# Database-owned Human vault and Obsidian synchronization

Date: 2026-09-11

Status: selected design; implementation pending. This is the active specification
under [ADR 0048](adr/0048-human-vault-database-authority-and-obsidian-sync.md).
It replaces the Markdown-authoritative Human reconciliation design. The user
selected database authority, browser authoring, an Obsidian extension distributed
from this repository, OAuth deployment configuration, server-only deletion, and
mutually isolated Human/Agent write capabilities. AI reads remain governed by the
existing read policy. This document does not claim any implementation or rollout.

## Ownership, hosting, and AI visibility

The database stores the authoritative revision of each enrolled Human note.
Browser saves commit there. Obsidian files are editable working copies whose
changes are submitted against their last synchronized revision. Unenrolled local
notes remain local, and Agent files retain their existing generated/read-only
projection workflow. Enrollment is explicit; installing the plugin uploads nothing.

The user's term `ai_readable` refers to the existing AI-read policy. In the
inspected code its actual name is `ai_read: allowed | forbidden`, resolved by
folder inheritance from private `folders.yml` and mirrored by `read_policy.py`.
There is no verified per-note `ai_readable` field. Preserve this fail-closed
policy; do not add an independently mutable boolean or treat an arbitrary
frontmatter value as an authorization override.

Hosting/enrollment and AI readability are separate. A Human operator may enroll,
read, and edit a Human note whose effective `ai_read` is forbidden. Agents may
read only allowed content and may never write Human content, regardless of the
read flag. This explicitly supersedes ADR 0014's exclusion-at-import rule for
enrolled Human content; its agent-facing query filters remain mandatory.

Apply audience-aware reads before listing limits, lexical/vector ranking,
hydration, counts, get-by-ID, references, export, and sync feeds. The Human read
capability reaches Human content; it does not grant privileged reads of Agent
review material. Existing ordinary `vault:read` may independently permit normal
Agent reads. No Human-only body, title, summary, vector result, or identifier may
leak through an Agent surface, dedup result, or derived page. Human sources remain
outside automatic Agent compilation and contribution-dedup comparisons initially.

AI readability controls agents consuming the corpus, not whether the configured
embedding provider processes a hosted note. Daily indexing covers live enrolled
Human notes for Human search; use existing deployment/provider policy and make
provider processing clear at enrollment. No additional provider is selected.

## Permission and OAuth contract

Use existing OAuth authorization-code/PKCE authentication, operator identity,
credential format, rotation, replay detection, and family-scoped entitlements
(ADR 0029). A public plugin has no client secret and obtains no DB credentials.
Human privilege must be granted explicitly by the operator, not acquired by
self-registration, client display name, requested scope, or ordinary consent.

Introduce a named **Human vault operator** grant comprising separate Human read,
create, edit, and move capabilities. Read covers Human retrieval and sync; edit
covers title/body/metadata; move covers path changes including rename. Use precise
verb scopes consistent with existing scope discipline; final wire names and
schema constraints are reviewed in phase B. This is a new granted role, not a
reinterpretation of generic Agent `vault:update` or `vault:write`.

| Operation | Obsidian Human family | Browser Human family | Agent families |
| --- | --- | --- | --- |
| Read enrolled Human notes | Human-read grant | Human-read grant | Only effective `ai_read: allowed` through ordinary read surfaces |
| Create/edit/move Human notes | Human operator grant | Human operator grant | Refused |
| Delete Human notes | Refused; no deletion capability | Separately granted Human deletion capability | Refused |
| Change Human AI-read policy | Refused through ordinary sync/content fields | Explicit existing governance/operator workflow | Refused |
| Mutate Agent notes, proposals, compilation, or review state | Refused | Refused by this family | Existing separately authorized Agent workflows |

The plugin requests only the minimum ordinary baseline needed to bootstrap, not
the default full baseline (`vault:write` and `vault:propose` are inappropriate).
The exact minimal authorization request must be exercised against the existing
OAuth SDK; do not invent an empty-scope flow without verifying it. Before the
operator grant, show "Connected; Human operator grant required" and sync nothing.
Use the existing family-aware operator grant/revoke workflow, extended with the
role. The grant must survive refresh and revoke from live/future credentials.
Reauthorization creates a new unprivileged family under current behavior.

Reject Human/Agent mutation-grant combinations during issuance, grants, refresh,
and request validation, including static credentials; scope-list filtering alone
is insufficient. Check the target's persisted collection ownership at the service
boundary for every verb. Agent contributions cannot choose Human paths; Human
operations cannot target or convert an Agent ID. Amendment application, summary
repair, retirement, promotion, and compilation must not provide indirect bypasses.

Use separate OAuth families for the Human browser, Agent workflows, and plugin,
with separate refresh locks/storage. The plugin family is issued as non-deleting;
the Human deletion grant belongs only to a separately authorized browser/operator
family. Enforcement trusts server-issued capabilities, not a request's User-Agent
or claimed application name. Revocation or missing scopes stops writes while
preserving pending local edits.

## Configuring a deployment

The plugin asks for a vault service URL and an explicit managed Human folder.
Discover authorization/resource endpoints from that deployment, including mount
prefixes; never hard-code Heroku, localhost, or one operator's identifiers.
"Any deployment" means any compatible deployment advertising the required sync
contract. Unsupported deployments get a clear compatibility message, not fallback
to generic contribution endpoints or direct database access.

Use the system browser, PKCE, state validation, exact registered callback handling,
and issuer/resource binding. Test an Obsidian protocol callback on supported
platforms; preserve base paths and validate discovery destinations. Production
requires HTTPS; any local-development exception is explicit and loopback-only.

Namespace identity maps, checkpoints, and credentials by deployment identity,
resource, local vault, and device. Changing the configured URL disconnects the old
session and requires a deliberate rebind; never forward its tokens or upload old
managed files automatically. Detect a deployment restored/replaced with different
sync history and require a safe resnapshot rather than trusting its old cursor.

Use an appropriate supported credential facility, with session-only storage as
the fallback when safe persistence is unavailable. Do not place secrets in notes,
Git-tracked configuration, logs, or automatically synchronized plugin settings.
Document the real storage and restart behavior for each supported platform and
prevent refresh-family tokens from being copied to another device. This is an
implementation verification requirement, not an assertion that one portable
secure-storage API is available. Each device authorizes independently.

## Local identities, revisions, and conflicts

Reserve plugin-managed stable identity metadata in Markdown (recommended key:
`VaultID`). Introduce it through the private governance schema, explicitly replacing
the previous prohibition on service identity in enrolled Human notes. Preserve
IDs through rename/move/edit. Device-specific base revisions, source hashes,
checkpoints, and the base text needed for conflict comparison belong in local
plugin state, not portable note content. Treat a duplicated ID as a conflict;
never pick a file or create a new server identity silently.

Creation uses a persisted operation ID for retry-safe allocation. First enrollment
previews exact files/metadata and establishes server IDs deliberately. Untracked
files inside the managed area are shown as pending enrollment, not auto-uploaded.
Browser-created notes download using their server IDs. Title edits do not silently
rename paths; rename and move are explicit operations within allowed Human roots.

| Change since synchronized base | Result |
| --- | --- |
| Server only | Download with an expected-local-hash guard |
| Local only | Submit with expected server revision; accept and acknowledge new base |
| Both sides | Preserve both versions and flag conflict; do not auto-merge initially |
| Neither | No content write or revision increment |

Only the conflicted note pauses. Include base/local/server content in a private
conflict view, and let the human compose or select a resolution that is itself
revision-checked. If the server changes again, require renewed resolution. Never
silently use last-write-wins or timestamps to decide authority. Restore/edit
history is required for authoritative Human writes; preserve prior content and
actor attribution within the vault boundary.

The concurrency token must change for edit, rename/move, and deletion, not just
the current content-only revision. Specify a sync/resource revision alongside
the existing `content_revision` if needed; do not assume their semantics match.
A title/path/policy change during an upload must not be missed by a body-only
comparison. Prevent case/path collisions and escaping paths/symlinks. A move
cannot change collection ownership. If a move changes effective AI-read policy,
refuse it in ordinary sync and direct the human to the explicit server/governance
workflow; do not silently publish previously hidden content by moving it.

## Deletion is initiated only through the service

Deleting a local file never sends a server delete request. Record a missing local
copy and offer restoration or opening the server note. Retain its identity and
base state so a later local file cannot masquerade as a new note. Do not repeatedly
restore a file the user removes without explaining the state.

A server deletion writes a durable tombstone/change record. On download, remove
an unchanged working copy recoverably through Obsidian trash. If local edits are
pending, retain them in clearly marked recovery storage excluded from sync and
enrollment. The deletion still takes effect remotely; do not upload those edits
to resurrect it. Intentional creation of a new note from recovered text is a
separate explicit action. Old operation retries cannot undo a tombstone.

Tombstones outlive incremental cursors, or cursor expiry forces a full resnapshot
that preserves pending edits and detects server absence. An expired client must
not interpret missing history as permission to recreate deleted IDs. Local
unsync/enrollment changes do not constitute remote deletion either.

## Synchronization and service contracts

Implement a dedicated authenticated Human API backed by the vault service layer.
The plugin needs: deployment/capability discovery; a consistent initial snapshot;
an ordered resumable change feed including moves/deletions; current document and
revision reads; revision-checked create/edit/move; and retry-safe acknowledgements.
The browser additionally uses the separately authorized delete operation. These
are new surfaces; recognized OAuth scope strings do not make them implemented.

Use a durable monotonic change position and opaque cursor tied to deployment and
authorization context. Do not use relevance-ranked search or `updated_at` alone
as a sync feed. Snapshot-to-feed transition must lose no changes. Persist
checkpoints only after local application/acknowledgement; tolerate repeated
delivery, pagination, reconnects, and response loss after a server commit.

Debounce Obsidian save events and poll remote changes while the plugin is active;
recommend a 60-second fallback/startup reconciliation, with bounded retries.
Initial vault-load events and downloaded writes must not become new local edits.
Use identity/revision/hash comparisons for echo suppression, and guard local
replacement against edits that arrive during network calls. Never keep a database
transaction open while waiting for provider, client, or filesystem work.

Do not run the old Human mark-and-sweep importer or an independent Human exporter
over enrolled paths. The plugin is their local synchronization owner. Agent export
remains separate and cannot write Human working copies. Other file-sync products
are not part of this protocol; do not sync device credentials/checkpoints between
devices, and document/test interactions before supporting multiple active file
writers over the same managed tree.

## Plugin packaging and platform support

Keep extension source in `clients/obsidian/` in this repository, including manifest,
build scripts, lockfile, README, synthetic tests, and instructions for installing
the built plugin into an Obsidian vault. It is an Obsidian extension, not a Codex
plugin. Document its movement with the vault during repository extraction. Public
source/builds contain no vault data, identities, tokens, or real-note fixtures.
No community-directory publication or new infrastructure is implied.

Use portable Obsidian and Web APIs first; avoid Node/Electron-only dependencies
in the sync core. Desktop is the initial verified target. Mark/document platform
support conservatively; claim mobile only after real-device OAuth return,
credential persistence, suspend/resume, file operations, and conflict tests pass.
No unconditional mobile background-sync guarantee: synchronization is active only
while the application/plugin can run. The service browser remains usable without
Obsidian running. Platform-specific adapters must not change server semantics.

The UI includes connect/disconnect, deployment identity, managed selection, grant
status, pending uploads/downloads, missing local copies, conflict resolution, last
successful synchronization, and open-in-browser. Show "Saved locally; upload
pending" distinctly from "Saved to service". Human web editing uses the same
revision semantics and displays the independent daily semantic-index state.

## Migration, verification, and delivery

Before enrollment, preserve private Markdown originals and rehearse the existing
Agent export separately. Preview selected Human enrollment; do not transfer all
private notes because Human-read can reach them. Preserve source metadata and
existing imported IDs when applicable. Cut over each selected set once, fencing
the obsolete importer before accepting web/plugin writes. Rollback after edits
requires reconciliation of accepted server revisions, not restoring old files and
restarting the importer.

Explicit vault Alembic work is required for reviewed identity/ownership invariants,
resource revisions/history, tombstones/change feed, role entitlements/constraints,
and job state as needed. Do not silently overload `source_sha256` or `origin`.
Private governance changes add managed identity rules and align read-policy
evaluation. No ORM, host-domain dependencies, or unreviewed new runtime packages.

Acceptance must cover two separately configured deployments, PKCE/state failure,
grant/revoke/rotation/reauthorization, incompatible grant combinations, and every
Human/Agent read/write combination through REST and MCP where exposed. Include
private Human notes in synthetic fixtures and test discovery, counts, references,
exports, dedup, compile, and retained-vector hydration for disclosure regressions.

Exercise create retries, moves/renames, ID collisions, concurrent local/browser
edits, lost responses, restart, missed changes, tombstones, local deletion,
server deletion with dirty files, expired cursors, and path/policy changes.
Prove the plugin token cannot delete or mutate Agent notes even with handcrafted
requests. Prove Agent tokens cannot create, edit, propose changes to, move,
promote, or retire Human notes. Read permission never becomes write permission.

Verify active sync makes no document embedding calls. Browser/plugin saves become
keyword-searchable immediately after server acceptance; daily full-text indexing
coalesces changes and retains older compatible vectors until replacement under
the [embedding specification](human-embedding-refresh.md). Preserve its race,
withdrawal, and provider-failure tests with the new Human read audience.
