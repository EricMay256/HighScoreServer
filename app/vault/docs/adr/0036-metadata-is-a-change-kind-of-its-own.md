# 0036. Metadata is a change kind of its own

Date: 2026-08-29

## Status

Accepted.

## Context

Every path that edits a stored note is a full replacement. `PUT /notes/{id}`
requires `title` and `body`; `vault_propose_note_amendment` requires them too.
`vault_propose_note_body_diff` and `vault_propose_note_span_edit` are narrower
but address only the body.

So there was no way to change a note's **edges** without resending the note.

This surfaced concretely. Fifty-two notes were contributed in one session, each
citing an earlier sibling, which left 77 one-way edges and nothing pointing
forward — a graph that reads as a contribution order rather than as a set of
relationships. Reconnecting it properly meant changing `related_ids` on 45
notes, and the only tools available made that 45 full replacements: roughly
54,000 characters of body text retyped through an API, where one wrong
character silently rewrites content while presenting as a metadata change. A
reviewer comparing 45 bodies by eye does not catch that, and neither does the
schema, because at the schema level a replacement carrying a slightly different
body *is* a valid replacement.

The cost also shaped the original defect. At contribution time the cheap thing
was a single backward link, because adding more later was disproportionately
expensive. The absence of this path is why the graph was thin in the first
place.

## Decision

Add `metadata` as a third `AmendmentProposalKind`, carrying only fields that do
**not** join `assemble_embedding_text`:

- `related_ids`
- `source_ids`
- `facets`
- `source_url`

Each is optional. An absent field means unchanged; an empty collection means
empty. Because `source_url` is nullable in storage, "clear it" and "leave it"
cannot both be spelled `null`, so the MCP tool carries an explicit
`clear_source_url` flag.

`tags` and `aliases` are deliberately excluded. Both are embedded — tags at the
weight ADR 0013 sets, aliases at weight A alongside the title — so changing
either alters what the note means to search. That requires a re-embed and a
dedup run, and it can collide. Excluding them is what lets this kind promise
that it cannot affect retrieval, and that promise is the whole reason it can be
cheap.

The database enforces the payload shape rather than trusting the application:

```sql
change_kind = 'metadata'
AND change <> '{}'::jsonb
AND change - 'related_ids' - 'source_ids' - 'facets' - 'source_url' = '{}'::jsonb
```

Acceptance reuses the existing machinery. A stored metadata proposal
materialises into an `UpdateRequest` built from the target with only the named
fields overridden — the same shape `_body_only_update` already uses for body
diffs. Because the embedding text is unchanged, the update path's existing
`embedded_text_sha256` comparison skips re-embedding on its own; no special
case is needed and no provider call is spent.

## Consequences

A caller with `vault:propose` can now change a note's graph position without
holding `vault:update` and without the ability to alter its content. That is a
genuinely narrower capability than the one it replaces, expressed as a narrower
payload rather than as a promise.

Review becomes tractable. A metadata proposal renders as the change — "these
four edges" — instead of as a document a reviewer must diff against the stored
one to find the difference.

The kind is not general. Editing tags still means a full replacement, and a
caller who wants to change a body *and* an edge needs two proposals or one
replacement. That is the intended shape: the cheapness is bought by the
narrowness, and widening the payload later would forfeit the guarantee that
this operation cannot touch retrieval.

Migration `0018_metadata_amendments` widens the two CHECK constraints. Its
downgrade deletes pending metadata proposals, because they cannot satisfy the
narrower constraint — safe because a proposal is inert by construction: it is
absent from search and dedup and has changed no document, so discarding one
loses a queued intention and never any corpus content. Decided proposals are
kept, since their record is history rather than pending work.

## Alternatives considered

**Only the proposal kind, without an applied counterpart.** Rejected, and the
first draft of this ADR had it the other way round — arguing that a direct path
under `vault:update` "serves the wrong caller" because that scope can already
replace the note. That reasoning is wrong, and inverted: it leaves the holder of
the *stronger* scope with only the *sharper* instrument. An operator with
`vault:update` who wants to add one edge would have to reach for
`vault_update_note`, resend the body, and take exactly the rewrite risk this
whole ADR exists to remove.

A capability that can replace a note should be able to do the narrower thing
directly. So all three scopes are served:

- `vault:propose` — `vault_propose_note_metadata`, queued for review.
- `vault:update` — `vault_update_note_metadata`, applied immediately under a
  revision compare-and-set.
- `vault:review` — no new tool. `vault_decide_amendment_proposal` is
  kind-agnostic and materialises a metadata proposal through the same
  `_update_request` branch; it needed only the ability to *render* one, which
  `amendment_proposal_change` now does.

**Allowing `tags` with an automatic re-embed.** Rejected because it makes the
operation's cost and failure modes depend on which fields a caller happens to
send — a metadata edit that sometimes spends a provider call and sometimes
returns 409 from the dedup gate is not the cheap, predictable operation this
was built to be.

**Doing it as a one-off script.** This was the immediate alternative and it
would have worked once. It leaves the next caller with the same problem, and
the problem is structural rather than incidental: the absence of this path is
what made the graph thin, so a script fixes the symptom on the day it is run
and preserves the cause.
