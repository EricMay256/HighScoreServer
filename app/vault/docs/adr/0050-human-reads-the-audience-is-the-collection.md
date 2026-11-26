# 0050. Human reads: the audience is the collection, and Agent workflows exclude it

Date: 2026-09-12

## Status

Accepted. Phase B2 of the [handoff](../human-vault-handoff.md). Builds on
[ADR 0049](0049-the-human-collection-boundary.md)'s ownership column and gives
`vault:human-read` its first consumer. No Human write endpoint exists because of
this decision.

## Context

ADR 0048 separates writers, not readers. An agent may read a Human note that
`ai_read` allows; a Human operator reads Human notes whatever their `ai_read`
policy; and Human notes stay out of contribution dedup and automatic compilation
initially. The sync specification asks for those audiences to be applied inside
the query — before limits, ranking, hydration, and paging — not to a page after
the fact.

After B1, `vault:human-read` existed in the schema and granted nothing, which is
the state a declared scope should not stay in. And the Agent workflows that read
the corpus as *input* rather than as a response — the dedup gate and compile
planning — still saw every note the read policy allowed, Human rows included.

## Decision

**The audience is chosen by route, and each route states it in its query.**
`/api/v1/vault/human/notes` and `/human/notes/{id}` require `vault:human-read`
and filter to `collection = 'human'` with no `ai_read` predicate. The ordinary
routes keep `readable_path_predicate`. Not an audience parameter on `/notes`: a
parameter is something the caller chooses, and which rows a request may see must
be decided by the scope that admitted it. Not a policy object either — two
predicates, each written where the query is built, are easier to audit than an
abstraction that decides for them. The Human routes read no Agent note; an Agent
id is not found there, as a Human id is not reachable through the Agent write
routes.

**Agent reads are unchanged.** Search, fetch by id, the listing, and edge
resolution may return a Human note `ai_read` allows. That is ADR 0048's reader
rule, and it holds without new code.

**Agent workflows exclude Human notes.** `find_similar` compares Agent notes
only, so a person's note can never be the reason an agent's contribution is
flagged or refused — a veto nobody could see. Compile planning reads
`note_states` and `note_frontier` over the Agent collection, and `write_page`
validates `source_ids` against the same map, so a Human id cited as a source is
unresolved rather than laundered into a page the agent surface serves.
`find_related_pages` and `list_pages` need no filter: they read wiki pages, and
ADR 0049's CHECK keeps a Human row from being one.

**The Human models extend the agent ones.** `VaultHumanNoteDetail` and
`VaultHumanNoteSummary` subclass the agent read models and add
`resource_revision`, so the two surfaces cannot drift on what a note is. The
agent models do not publish `resource_revision`; no agent client writes against
it.

**Separate quota buckets**, `human_get_note` and `human_list_notes`, priced like
the agent reads they mirror, so a principal holding both read scopes does not
spend one allowance on the other surface.

**Human search is deferred to phase C.** Its semantic arm needs the daily Human
embedding job, and the browser is its first consumer. Listing and fetch are what
a sync client and a browser need first.

## Consequences

A contribution that restates a Human note is not flagged. That is the selected
behaviour until someone decides Human notes should take part in dedup, which
would need its own calibration question: a person's note and an agent's note
saying the same thing are not the duplicate the gate was measured for.

An agent can find an `ai_read`-allowed Human note through search and then be
refused as not found when it tries to update it. ADR 0049 already flagged that
the refusal may want a clearer error once this happened; it now can.

`vault:human-read` returns notes governance hides from agents — notes about real
people among them — to any credential holding it. It remains an operator
entitlement that no OAuth consent can grant, and a Human family is authorized
requesting `vault:read` alone.

The listing's cursor codec is shared, so a cursor from one listing is accepted by
the other in the same sort. It names a position and nothing more, and each query
applies its own audience, so it discloses nothing and at worst starts the walk
from the beginning.

The MCP surface is unchanged, so no client's `knowledge-vault` skill needs an
update.
