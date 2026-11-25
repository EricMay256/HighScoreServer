# 57. The activity log is derived from the audit trail

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented.

## Context

Karpathy's LLM wiki keeps `log.md`, an append-only timeline of ingests, queries and
lint passes, so the model knows what was done recently. The vault records every
governed write in `vault_audit_events`, which is never pruned (ADR 0002), but no agent
can read it.

Two shapes were considered. A log derived from the audit trail cannot be spammed and
needs no new write scope, but it shows only governed operations. Agent-written entries
could also record reflections that produced nothing and queries never filed, at the
cost of a new write channel from one agent into every other agent's context.

## Decision

**The log is derived and read-only.** A `vault_recent_activity` MCP tool under
`vault:read` returns one line per recent event: date, operation, outcome, target
title and id. The exporter writes the same lines to `Agent/_log.md`, generated like
`_index.md`, with timestamps taken from the events rather than the clock.

Events whose target is unreadable under `readable_path_predicate`, or is in the Human
collection, are omitted. Agent-written entries are not added. Reopen that only if the
week of measurement in ADR 0053 shows the missing entries matter.

## Consequences

The skill tells agents to read the log before gardening or reflecting. The tool is
bounded in lines and bytes and counts against `test_mcp_budget.py`. `Agent/_log.md`
joins the export's `expected` set, or the prune sweep deletes it.
