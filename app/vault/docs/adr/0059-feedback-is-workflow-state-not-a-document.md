# 59. Feedback on a note is workflow state, not a document

Date: 2026-11-25

## Status

Accepted 2026-11-25. Not implemented. **Requires a vault Alembic revision.**

## Context

An agent that uses a note learns whether it helped, and has nowhere to say so. ACE
(ICLR 2026) keeps helpful and harmful counters on each playbook entry. It also found
that rewriting a whole context loses detail, so small signals beside an entry beat
edits to it.

The vault already distinguishes proposing from changing. `vault:propose` writes
untrusted workflow records and never corpus content (ADR 0028).

## Decision

**A `vault_rate_note` tool under `vault:propose` records `helped`, `wrong` or
`outdated`, with an optional comment of at most 500 characters, against a note's
`content_revision`.**

- Stored in `vault_note_feedback`. The note id is a correlation id, not an FK,
  following ADR 0002, so feedback outlives a retired note.
- One verdict per (principal, note, revision, verdict). A resend replays it.
- Feedback never alters the note, is never embedded, and does not appear in search
  responses (ADR 0031's selection test is not met yet).
- Listings show counts, not comments. A comment is untrusted text, like a review
  case's candidate.

`wrong` and `outdated` surface in the health report and the review queue, where a
human decides between an amendment, a supersession (ADR 0060) and nothing.

## Consequences

The signal is only as honest as its raters, and every rater is an agent. That is why
feedback routes to a human rather than acting on its own.
