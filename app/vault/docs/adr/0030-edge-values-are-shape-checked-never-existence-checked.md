# 30. An edge value is checked for shape, and still never for existence

Date: 2026-08-26

## Status

Accepted 2026-08-26. Answers the question ADR 0025's 2026-08-26 amendment left
open, and narrows nothing that ADR 0025 decided.

## Context

ADR 0025 decides that `related_ids` and `source_ids` carry no foreign key: a
contribution may legitimately reference a note that is archived, flagged,
retired, or not yet written, so a dangling edge is normal rather than
corruption. That decision is about **existence**, and it is not in question
here.

It has been read, in this codebase, as though it were about *validation in
general*. `VaultDocumentContentRequest.validate_ids` checked only that values
were non-empty and unique. The field description said "Not checked for
existence" and stopped. `remap_vault_reference_ids` excluded non-id values from
its dangling-reference report — correctly, since they are not dangling ids —
and in doing so became the third place that saw them and said nothing.

The result was twenty-one `[[Title]]` strings sitting in `related_ids` in
production, put there by `import_vault_wiki` and written back out by the
exporter, so the corpus round-tripped them for four days looking correct.

**Shape and existence are different questions.** "555-0199 is disconnected" and
"banana is not a phone number" are not the same finding, and a rule that
declines to make the first says nothing about the second. `[[Operating the Agent
Knowledge Vault]]` is not an id pointing at a note that does not exist. It is
not an id.

### Why the obvious validator is the wrong one

Every document id in the corpus is `uuid4().hex` — 32 lowercase hex characters.
The service mints it (`service.py`), callers cannot choose one (the compile
path's `page_id` must already name a live page), and the Stage-A generations the
corpus replayed used the same shape. So `^[0-9a-f]{32}$` is exactly correct
today.

That is the problem with it. `vault_documents.id` is `TEXT` with no format
CHECK, deliberately — nothing in the schema claims the format is permanent — and
a validator in the **request model** is a term of the public wire contract. Ids
gaining a prefix, or becoming ULIDs, would then be an API change: old clients
sending ids the vault itself issued would start receiving 422s, and the rule
would have to be versioned and migrated rather than simply changed. That is a
large, permanent commitment bought to catch a mistake whose actual shape is much
cruder.

## Decision

**A value in `related_ids` or `source_ids` is rejected when it is plainly a
*name* rather than an id: when it contains whitespace or a square bracket.**
Nothing else about its form is asserted, and its existence is still never
checked.

`wikilinks.looks_like_a_name` is the single statement of the rule.
`api_models.validate_ids` raises on it, so both transports refuse at the request
boundary before any database work; `export._warnings` reports it, because that
layer sees rows the API never validated — imported, script-written, or older
than this ADR.

Whitespace and brackets are what every written name eventually carries and no id
ever has. The rule is stated as a property of *names*, not of ids, which is what
keeps it independent of the id format.

### Not a database CHECK

The constraint lives in the request model rather than in DDL, and the ordering is
the reason. A CHECK on `related_ids` could not have been applied while the
thirteen bad rows existed — the migration would have failed on them — so it would
have needed the repair to have run first, in production, before the safeguard
could land. A validator is available immediately and independent of the data.

The second reason is scope. A CHECK binds every writer including the repair
script and the importer, which have to be able to *read* bad values in order to
fix them. The API boundary is where a caller's claim arrives, and that is exactly
what this rule is about.

## Consequences

**A request carrying a title or a wikilink now returns 422 where it returned
200.** The error names the offending values. No client in this repository sent
one, and no stored idempotency key covers a body that would now fail.

**The residual is accepted and worth stating.** A bare slug —
`operating-the-agent-knowledge-vault` — has no whitespace and no bracket, so it
passes this rule and is stored like any other unresolvable id. Catching it
requires knowing the id format, which is the trade this ADR declines. The
exporter omits it from `SeeAlso`/`Related` exactly as it omits any id that does
not resolve, so the failure degrades to an absent link rather than a broken one.

**The agent-facing documentation carries the rule**, in the field description and
in both MCP tool docstrings, because the clients most likely to make this mistake
are models reading a corpus where `[[...]]` is how a note is normally named. A
constraint an agent cannot see is one it will violate.

**`source_ids` gains the check for free** and loses nothing: compilation already
refuses an unresolved source (ADR 0027), so shape was the only failure left that
could reach a row.

**What stays open.** Whether the id *format* should ever be asserted anywhere —
in the schema, or in the wire contract — remains undecided, and this ADR is
deliberately arranged so that answering it later is additive rather than a
reversal.
