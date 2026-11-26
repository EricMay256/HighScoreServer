# 53. The agent loop lives in the client, and the librarian is not built

Date: 2026-11-25

## Status

Accepted 2026-11-25. Rejects ADR 0043 (librarian runs as persisted drafts) and
retires `librarian-plan.md` as a plan. ADR 0044 (proposal lineage) is unaffected
and stays unscheduled.

## Context

The vault exists to improve the user's agents: memory that persists across sessions
and providers, judged by value per token of context it costs.

A 2026-11-25 review found effort concentrated on write correctness (idempotency,
locks, review, provenance) and almost none on whether notes are written at the right
moments, read, or kept true. Nothing prompts an agent to search when it hits a known
problem, or to record what it learned once the problem is solved.

ADR 0043 answered a related need with server-side machinery: session, event, artifact
and invocation tables, two model runners, a console, and USD-5 cost accounting. That
rebuilds what a coding-agent client already has — sessions, hooks, scheduled
routines — around tools the vault already exposes.

Two external findings shaped the replacement:

- Anthropic's *Components of agent memory* (2026-11-06) found that a "record only
  failures" rule *reduced* the benefit of memory. A note written at the moment of
  failure has neither the working fix nor its evidence. Recorded as vault note
  `ea95b7b5dbf84bc2b0ad9d9eb398ef59`.
- Karpathy's LLM-wiki pattern (2026-04) names ingest, query and lint as the
  operations, and files good answers back into the store. Its community found that
  scripted checks catch drift an LLM health check passes.

## Decision

**The loop runs in the client. The server supplies read surfaces and governed
writes, and gains no workflow state.**

The `knowledge-vault` skill and its hooks ship together as a Claude Code plugin from
knowledge-platform. The behaviour is specified provider-neutrally, so other clients
can implement equivalents or rely on the server instructions alone:

| Behaviour | Claude Code event |
| --- | --- |
| When a tool fails, prompt a vault search for that error, once per distinct error per session, ignoring informational failures. Never require a write. | `PostToolUseFailure` |
| After a substantial stretch of work, block the stop once and ask for reflection: search, prefer amending to adding, contribute at most one or two claim-titled notes, or say "nothing durable". | `Stop`, guarded by `stop_hook_active` |
| At session start, inject the titles of promoted notes for the project, kept tiny and labelled as data (ADR 0061). | `SessionStart` |
| Before compaction, mark a reflection as due. | `PreCompact` |

**Query filing.** An answer built from two or more notes that then works in practice
is contributed as a note whose `source_ids` name the notes it drew on. The compile
planner offers it as a `new-source`. An answer that restates one note becomes an
amendment to that note instead.

**The garden routine** spends spare quota on the corpus, under an action budget. It
reads the health report (ADR 0056) and the activity log (ADR 0057), then works
through summaries, links, compile items, near-duplicates and gaps in that order. Its
credential holds `vault:read`, `vault:write`, `vault:propose` and `vault:compile` —
never `vault:update`, `vault:delete` or `vault:review`. The human settles its
proposals in the review console, which keeps ADR 0021's boundary intact.

## Consequences

- No librarian schema, scopes (`vault:librarian`, `vault:librarian-run`), runners or
  provider billing are built. ADR 0043's reasoning remains as a record.
- Hooks raise write volume while `flag_at` catches only identical embeddings. The
  mitigations are the reflection prompt's amend-first wording, the contribute-time
  similarity warning (ADR 0032's 2026-11-25 amendment), and recalibration (ADR 0016's
  2026-11-25 amendment).
- Distribution and versioning of the plugin belong to knowledge-platform. Agreement
  between the skill and the MCP surface is still checked by hand (host AGENTS.md).
- Success is measured in the client over a week of use: contributions per session,
  flagged outcomes, contributions at or above the warning threshold, and reflections
  ending in "nothing durable".
