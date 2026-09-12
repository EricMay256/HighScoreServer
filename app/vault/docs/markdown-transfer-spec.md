# Markdown transfer: export rehearsal and Human reconciliation

Date: 2026-09-11

Status: historical Human import design, superseded by
[ADR 0048](adr/0048-human-vault-database-authority-and-obsidian-sync.md) and the
[active sync specification](human-vault-sync-spec.md). Section A's existing Agent
export rehearsal remains applicable. Sections B onward preserve the earlier
Markdown-authoritative design for context; do not implement their Human sweep or
replica-write prohibition for database-owned enrolled notes. The current start
point is the [Human vault handoff](human-vault-handoff.md).

## A. Rehearse Agent export

Use `scripts/export_vault_markdown.py` unchanged first. It reads the selected
database directly, requires an explicit output root, writes Agent-owned paths,
and needs no embedding provider. Confirm `DATABASE_URL` versus
`VAULT_DATABASE_URL` through the script's redacted target report; never print a
credential-bearing connection string into shared records.

1. Choose a new private output directory outside the HSS tree and outside the
   live Obsidian vault. Record the runtime revision, redacted DB target, and
   resolved directory in a private exercise report.
2. Run `python -m scripts.export_vault_markdown --out <private-output-root>`.
   This is the existing dry-run command, with a placeholder that must be replaced.
   Inspect counts, warnings, dropped fields, and proposed orphan removals.
3. Repeat with `--apply`, without `--prune`. Inspect representative note, wiki,
   promotion-candidate, and flagged records when present. Check frontmatter,
   status, Unicode, ID preservation, and ID-to-wikilink rendering. Record missing
   categories as unexercised; use synthetic fixtures for their automated coverage.
4. Run the private governance validator/linter in checking mode against the
   projection in its required schema context. Investigate reported key-order
   drift. If normalization is necessary, include it in the repeatability check;
   do not count an exporter/linter rewrite loop as stable output.
5. Export again against an unchanged corpus. Compare per-file bytes/hashes and
   require no note rewrites. Activity during the run is allowed, but comparing
   two runs only establishes repeatability when their underlying content matches.
6. Exercise pruning in a disposable synthetic projection. Verify only exporter-
   owned paths are candidates, that Human/unowned files survive, and that a failed
   export cannot trigger cleanup of the last completed projection.
7. Open the completed output with the service unavailable and record what remains
   useful: text, metadata, index, and resolvable links. Record omitted metadata and
   unsupported assets. Exported Markdown is a reading projection, not a complete
   DB backup of credentials, audit, review, and other workflow state.

This phase does not copy into the live `Vault/Agent/` tree or enable a schedule.
That cutover needs a reviewed comparison with existing files and confirmation
that the Stage-A Agent writer is retired. Preserve the prior private projection
until the replacement has been validated. Never prune `Human/`.

## B. Select what is published

A private, versioned publication manifest identifies one source vault and the
selected Human paths or folder prefixes. Keep this outside note frontmatter so
the pilot does not require editing every canonical note or extending its schema.
Folder selection intentionally includes future notes within that selected folder;
explicit-file selection does not. The preview must make that distinction visible.

The eligible set is the intersection of selection, source `ai_read` policy, and
the runtime's read policy. Selection can narrow access, never widen it. Evaluate
folder inheritance using the existing governance resolver, not a new approximate
prefix matcher. Compare source policy/version with the runtime's reviewed policy
and refuse a mismatched run before sending bodies. The implementation must include
a cross-repository compatibility check; HSS's internal Python/SQL agreement test
alone does not establish source-policy agreement.

Publish no files by default. The operator selects the pilot using a private
preview listing selected, excluded, invalid, changed, and removal-candidate paths.
Do not collect excluded note bodies just to preview them, or put private paths
and note titles in public CI artifacts. An excluded link target is not imported
transitively. The hosted surface continues to expose only the current shared
readable corpus; person-specific private Human browsing belongs to option 3.

Reject paths outside `Human/`, path traversal, absolute paths, escaping symlinks,
and ambiguous filesystem aliases/case collisions. Preserve vault-relative POSIX
path spelling so the stored path agrees with governance. A missing root, partial
scan, unreadable file, or unresolved sync-conflict file fails validation; it is
not evidence that the corresponding replica should be deleted.

## C. Reconciliation contract

Use a dedicated operator import service and adapter, not the contribution
endpoint. Human replicas preserve independently authored nearby notes: no semantic
duplicate rejection, automatic merging, AI rewriting, or Agent-note type coercion.
Validate against source governance and existing database field constraints.

The initial transport recommendation is a local operator command using the
vault's service layer and a deliberately configured database connection, matching
the existing exporter. The private runner supplies validated input from its local
source tree. This is an operator tool with database authority, not an agent MCP
capability. The implementation must record packaging and minimum DB grants before
deployment; never copy the private corpus into HSS to make imports convenient.

An authenticated import/export API is an alternative if direct DB access is
unsuitable for the actual runner. It requires a separate reviewed scope/transport
design and is not a prerequisite for the pilot. Do not use `vault:write`, direct
note replacement, or a browser credential as implicit import authority.

### Identity, changes, and links

- Upsert by the existing unique `vault_path`, with generated service IDs and
  `source_sha256` containing the hash of the bytes actually parsed. Unchanged
  input is a content no-op regardless of which authorized operator runs it.
  Source identity must not depend on a principal-scoped contribution ledger.
- A content update keeps the ID and increments `content_revision` through a
  dedicated replica replacement path. Preserve validated source metadata and
  record import/check times separately from authoring and content-change times.
- Before creating/removing rows, pair a missing old path with a new path only
  when their identical source hash gives an unambiguous one-to-one match within
  this source. Preserve the ID on that move. Never infer a move from title alone
  or choose arbitrarily between identical files.
- Rename-plus-edit cannot be inferred reliably from identity-less Markdown.
  The dry-run reports it as removal plus creation under ADR 0012. For a known
  move, support an explicit operator-reviewed old/new path mapping with expected
  previous hash/ID; revalidate it at apply. Otherwise treat it as a new identity
  and report reference consequences before removing the old row. Do not add IDs
  to canonical Human frontmatter without a separate governance decision.
- Resolve links after the full candidate inventory exists. Preserve original
  frontmatter references and report ambiguous/unresolved links. A short name that
  matches multiple documents never picks the first hit. Import selection does
  not expand to satisfy a link. Do not silently rewrite unrelated Agent content
  when a Human path disappears.

### Atomicity, errors, and removals

1. Capture an inventory and immutable input bytes, validate the full selected
   set, and produce a plan. Immediately before apply, verify inputs still match
   the captured hashes; refuse/replan changed input. A later source edit belongs
   to the next run and must not be reported as observed by this run.
2. At the initial corpus size, prepare parsing outside the DB transaction. Import
   makes no embedding calls; it records changed embedding inputs for the daily
   job. Under the existing corpus lock, recheck the previous source
   generation and replica hashes; commit the planned replica changes and completed
   run state together. A competing run must replan rather than applying stale
   removals. Do not hold a database connection while scanning disk. Keep blocking
   filesystem/parser work off async handlers. Embedding updates use their own
   short transactions and must not overwrite content or import completion state.
3. A failed/incomplete scan or apply leaves the last completed replica generation
   intact and records failure separately. No sweep follows a partial run. Future
   batching must preserve this visibility contract before replacing it.
4. Removal is limited to previously imported rows owned by this configured source
   and its previously published selection. Never sweep all `Human/%` merely
   because the current selection is small. Agent/database-owned rows are ineligible.
   Selection shrinkage, deleted files, and policy changes need distinct reasons.
5. Preview exact removals. For unattended operation, persist an operator-reviewed
   minimum inventory and maximum removal count/fraction; exceeding either refuses
   cleanup and reports intervention required. An empty/missing source never grants
   implicit permission to remove the corpus. A deliberately empty publication set
   requires a separate explicit unpublish operation with its exact targets.
6. Tightening read policy must withhold affected rows at query time before cleanup
   completes. Sweep failures cannot become a visibility loophole. Do not bypass
   pending-review evidence protection to clean up a row; report the conflict.

### Retrieval and mutation boundaries

Human content imports continuously while the runner is online. Browsing and
lexical search reflect each completed import; document embeddings refresh only
in the daily job. Use the existing title, aliases, tags, summary, and full body
assembly and `embedded_text_sha256`. Summary-only embedding is rejected. Existing
authored summaries remain ordinary input fields; no summary-generation job is added.

When assembled text changes, retain the last successfully installed embedding
for its compatible profile until a replacement is available. Keep its original
input hash and embedding timestamp; never relabel it as current. Search ranks
using that vector but hydrates the latest readable document text and preview.
New notes without a vector are lexical-only until their first daily refresh.
Metadata changes outside embedding input do not enqueue work, and an input that
returns to the installed vector's hash needs no refresh.

This deliberately permits stale semantic matches for Human replicas: removed
concepts may still retrieve a note and added concepts may not. It does not relax
Agent write/dedup consistency. Access-policy changes and removal from publication
apply to retrieval immediately when committed, including vector retrieval and
result hydration; no retained vector or late job result may bypass them.
Do not discard a valid older vector merely because a scheduled refresh fails
or becomes overdue. Report its age and pending/error state instead.

Long Human notes may exceed current embedding limits. Preserve their full text,
report missing or outdated semantic coverage as appropriate, and retry only
transient provider failures. Retain an existing compatible vector if the new
body is too long; a new oversize note remains lexical-only. Never
truncate the canonical text or repeatedly retry permanent oversize input. Chunking
remains governed by ADR 0034 and needs measured justification.

Add collection filtering to both search arms and to listing. Human replicas are
searchable but initially excluded from automatic wiki compilation and the Agent
contribution dedup comparison set. This prevents new import data from silently
changing rejection thresholds or being copied into durable Agent synthesis before
withdrawal/provenance behavior is designed. Audit all planner, source-fetch, and
dedup queries; a UI-only exclusion is insufficient.

At the service boundary, prevent ordinary updates, summary repair, amendment
creation/application, promotion, retirement, or compile operations from mutating
a Human replica or transferring it into Agent ownership. The dedicated reconciler
owns its lifecycle. Return an explicit ownership conflict under existing error
conventions. Keep authorization checks before disclosing ownership details.

## D. State, scheduling, and failure reporting

New durable transfer state requires a reviewed vault Alembic migration. Design
records sufficient to represent the source identity, approved selection/policy
versions, previous completed generation, document ownership/membership, start/end
times, outcome, counts, and sanitized failure category. Reuse existing document
fields where their semantics fit; do not overload `origin`'s closed vocabulary or
use `updated_at` as the last successful source check. The exact table/API schema
is an implementation deliverable reviewed before migration, not an existing API.

Run on one existing operator machine with the private source vault available.
The runner must not assume the Heroku application has a copy of local Markdown,
or clone that repository during deployment. Choose the scheduler for the actual
OS. Continuous import means a running process with short polling or debounced
file notifications while online, not an hourly or daily import. Recommend a
60-second reconciliation poll initially, reusing the complete-scan safeguards;
add a watcher only if measured latency or scan cost justifies it. Handle editor
temporary writes without importing partial content, and reconcile on startup/wake.
Daily embedding work has a separate schedule; Agent export has its own cadence.
Record actual settings and overdue thresholds before installation. Prevent
overlapping jobs of the same kind and do not replay every missed interval.

The [daily Human embedding job specification](human-embedding-refresh.md) defines
selection, result validation, recovery, and ordinary-request versus provider Batch
API recommendations. It runs from imported database content and does not require
access to the Markdown filesystem. Source freshness and embedding freshness remain
separate even if one machine initially runs both jobs.

Import, embedding refresh, and export succeed independently. Mark a Human source checked only when
the selected scan and apply finish. Mark an export delivered only after local
files are written and validated. Reading the DB snapshot alone is not proof of
delivery. If the hosted browser displays export status, report a completion
receipt through the operator path after local success; failure to report leaves
status unknown/old. Never claim delivery to other devices from one runner's receipt.

For recurring export, write into staging, validate, then publish into only the
owned Agent directories, with recoverable prior output and completion metadata
outside the note files. If atomic whole-tree publication is unavailable on the
chosen filesystem, document the bounded replacement window and recovery process.
Do not call the existing per-file exporter atomically published merely because
its database read uses REPEATABLE READ. Prune only after successful publication.

Define content byte stability separately from a status manifest that naturally
changes with run time. Keep the last good files on failure and provide a visible
local last-export timestamp. Source Markdown plus this projection gives offline
reading as of their respective observations. It does not synchronize offline DB
edits and cannot restore the complete service; retain the existing database backup
and restore procedure independently.

## Required verification and handoff evidence

Use synthetic fixtures in public tests. Cover unchanged retries under another
operator, same-path edits, unique moves, duplicate-hash ambiguity, rename-plus-edit
with/without explicit mapping, invalid Markdown, source changes during preparation,
interrupted scans, concurrent runs, incorrect roots, and deletion thresholds.
Verify excluded paths across import, search, list, get-by-ID, and replica-write
guards; a selection manifest cannot override a denial. Test withdrawn selection
without removing unrelated Human or Agent rows. Test retention of older vectors
across edits/failures, daily coalescing, new-note lexical-only coverage, guarded
replacement, and immediate exclusion after withdrawal. Test that imported sources
stay out of dedup/compile. The daily-job specification adds restart, race, and
batch-result acceptance cases.

Export checks cover byte stability, Unicode, links, reported metadata loss,
consistent reads during concurrent service edits, staging failure, and scoped
pruning. Migration tests cover upgrade and schema agreement using the separate
vault lineage. Run browser scenarios from the parent handoff, existing relevant
vault tests, and the repository-wide Ruff gate after code implementation.

The private completion record identifies revisions, redacted targets, selection
and policy versions, counts, warnings, exact commands executed, output location,
last successful run, runner schedule, and how to disable it and recover prior
output. Report verification gaps explicitly. Do not claim real-world continuity
from tests alone or claim an export rehearsal from a dry-run alone.
