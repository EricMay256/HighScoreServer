# Daily embedding refresh for Human notes

Date: 2026-09-11

Status: selected behavior, implementation pending. Supplements the
[active sync specification](human-vault-sync-spec.md). Browser saves and active
Obsidian synchronization update authoritative Human content; changed documents
are embedded daily using their latest accepted server text. Keep the previous
successful compatible embedding until a replacement is installed. Summary-only
indexing is rejected.

## Three independent clocks

| Operation | Cadence | What becomes current |
| --- | --- | --- |
| Human browser saves / Obsidian sync | Browser acceptance immediately; continuous active plugin sync with 60-second fallback poll | Stored content, browser, lexical index |
| Human document embeddings | Once daily; only missing/changed embedding inputs | Semantic representation of selected documents |
| Agent Markdown export | Independently configured | Local reading projection of Agent content |

Daily refers to document indexing, not query embeddings. Query-time search and
Agent contribution/update behavior retain their existing contracts. A note edited
twenty times between daily runs is one latest-input work item, not twenty calls.
No embedding call is needed to check a hash, and an unchanged day makes no
document embedding requests. Input remains title, aliases, tags, authored summary,
and full body under ADR 0013. Do not generate summaries as part of this job.

## Recommended first implementation

Use a restartable operator command against the existing database and embedding
adapter. Start with one daily scheduled invocation, one job lease per profile,
and small ordinary API requests containing multiple documents. No new queue
service, provider SDK, or application web request is needed. The command reads
the authoritative database corpus independently of the machine running Obsidian.
Offline/unaccepted/conflicted local edits are not indexed until accepted by the
service. Closing Obsidian does not delay indexing of accepted browser saves.

On an existing Linux runner, a systemd timer is a candidate; on Windows use Task
Scheduler. These are deployment recommendations, not installations or assumed
available services. Choose an explicit timezone and daily slot when configuring
the runner (for example 03:00 local time if it is normally awake). Record the due
slot durably so restart/DST behavior cannot launch duplicate daily work. After
missed days, do one catch-up against the latest corpus, not one run per missed day.
If the current machine is rarely online, prefer an already available always-on
execution facility; adding a service/add-on or a new credential grant needs its
own deployment decision.

## Work selection and result installation

1. Select only live enrolled Human documents available to the Human indexing
   workflow whose active-profile vector is absent, has unknown provenance, or
   hashes differently from the
   currently assembled input. Track pending-since separately from embedded-at;
   a further edit must not hide the age of an outstanding refresh obligation.
   This includes AI-excluded Human notes used by Human search; `ai_read` filters
   Agent retrieval, not Human indexing eligibility. See the active specification's
   provider-processing and audience rules.
2. Snapshot each selected document's ID, ownership/lifecycle generation,
   content revision, input text/hash, profile, and assembly version. Group bounded
   inputs by model/profile. A row becoming dirty after selection waits for the
   next daily cycle; do not chase every edit throughout the day.
3. Persist the job and claimed items before provider work. Release database
   connections and use the async provider outside transactions. Keep independent
   bounded request timeouts/retries for this job; do not widen interactive search
   timeouts or hold the corpus lock during network calls.
4. Validate each returned vector's identity, dimensions, and values. In a short
   transaction under the corpus lock, re-read the target and recheck live eligibility,
   ownership, current profile, and assembled input hash. A metadata-only revision
   change may still accept the result if the actual embedding input is identical.
   If input changed, discard that obsolete result and leave current work pending.
   Never overwrite the new text or stamp the returned vector with a newer hash.
5. Install the vector and its real input hash/time atomically, without changing
   content revision or content-change time. Duplicate result delivery is a no-op.
   An older job cannot overwrite a newer accepted result or resurrect a deleted,
   no-longer-Human-owned, or no-longer-eligible note. Restricting `ai_read` does
   not erase the Human-readable record or authorize its deletion.
6. Persist outcomes per item: installed, already current, obsolete, withdrawn,
   transient failure, or permanent input failure. Successful independent items
   survive partial job failure. Failed or invalid items retain the previous
   embedding and do not block browser saves, local synchronization, or successful items.

Use a durable lease with expiry/recovery, rather than process memory or a DB
transaction held for the entire job. Bind installation to the active job claim
as well as current content so a worker continuing after lease loss is harmless.
Reconcile durable results on restart before submitting again. Exactly-once paid
provider execution cannot be promised after an ambiguous network response; bound
retries, record uncertainty, and prevent duplicate local installation.

Retry transient failures with bounded backoff within the scheduled run. On
exhaustion retain pending work for the next daily cycle. Do not retry permanent
oversize/invalid input until the relevant input or profile changes. A manual
recovery invocation is an explicit operator exception, never a side effect of
ordinary saves, synchronization, browsing, or a file watcher.

## Ordinary request batching versus provider Batch API

**Recommended initially: multiple inputs per ordinary embedding request.** The
existing `OpenAIEmbeddingProvider` already accepts sequences and defaults to 128
inputs per request. Choose conservative groups using both item and token budgets;
128 long Human notes are not automatically a valid request. Isolate permanent
bad inputs so one cannot repeatedly fail an otherwise useful group. The API
supports arrays of inputs with per-input and aggregate token limits; verify
current limits at implementation time. Grouping requests reduces round trips;
the reduction in repeated indexing comes from daily coalescing.
[OpenAI embeddings reference](https://developers.openai.com/api/reference/resources/embeddings/methods/create)

**Optional later: OpenAI Batch API.** Official documentation checked 2026-09-11
supports embedding requests with a 50% discount and a 24-hour processing window.
Results are joined using `custom_id`, not output order; expired batches can have
successful partial results. This requires uploaded request files and durable
submission, polling, result, and error bookkeeping.
[OpenAI Batch guide](https://developers.openai.com/api/docs/guides/batch)

For that alternative, persist an opaque request ID mapping to each exact input
snapshot; for multi-input requests also map the response input index. Keep files
and diagnostics private and define cleanup of local and provider artifacts.
Recover submitted batch IDs before resubmission and retry only unresolved items
against their latest eligible input. Apply the same installation guards above.
A batch-submission success is not an embedding-refresh success.

Daily submission plus a 24-hour processing window can approach two days of lag
for a change just after the previous cutoff, before failures or polling delay.
This is a consequence of the schedule, not a freshness guarantee from the provider.
Use this option only when measured volume/cost makes the extra state and delay
worthwhile. It is not selected for the initial pilot.

## Freshness, failure, and access

Expose content saved-at, device synchronized-at, vector generated-at, whether the vector input matches
current content, and pending/error state separately. A new note is "Awaiting
first semantic index" and remains searchable by keyword. An edited note uses its
last vector with "Semantic index awaiting daily refresh". Overdue work adds a
health warning; age alone does not evict the vector. A note unchanged for months
can have an old but fully current vector, so embedded-at alone cannot flag it stale.

Latest text is always hydrated through current read policy. Older vectors may
retrieve concepts removed from that text or miss newly added concepts; that lag
is accepted for Human retrieval. Deletion and audience read-policy restrictions
are not delayed until daily indexing. Removing AI readability immediately hides
the note from agents while preserving Human access and indexing. Filter retained
vectors per requesting audience and revalidate in-flight results against live
Human eligibility. Unknown-provenance or incompatible-profile vectors
are not trusted as a last successful compatible embedding.

Record pending counts, oldest pending age, installed/obsolete/error counts, last
job attempt and completion, and provider usage without logging note content.
Keep existing request-level `vector_status` semantics: a working query embedding
provider does not mean every document vector is current. If jobs repeatedly find
notes changed in flight, report starvation rather than silently claiming success.
Additional durable state requires reviewed vault Alembic revisions.

## Acceptance evidence

Verify multiple edits collapse into one latest-input request; unchanged metadata
and reverting to the installed input hash make no request; a first Human save works
without a provider; and failed refreshes preserve an older vector. Verify current
text is shown for an old-vector match. Verify changes during provider work reject
obsolete results while metadata-only changes do not cause needless replacement.
Test partial failures, expired leases, duplicate/late result delivery, restart,
missed daily slots, and input-size failures. Confirm deletion prevents discovery
and reinstallation immediately after its transaction, and an AI-read restriction
prevents Agent discovery while Human search still works. Confirm Agent consistency and
query embedding behavior remain unchanged. If the optional Batch API is selected,
add reordered outputs, partial expiration, and submission-recovery tests.
