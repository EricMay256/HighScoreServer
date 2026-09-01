# 0041. Human-authored notes in the vault

Date: 2026-08-30

## Status

**Deferred.** Recorded so the question is not answered by drift.

## Context

The vault's write path is built for agent contributions, and every governing
mechanism assumes that author. The dedup gate scores a contribution against the
corpus and flags a near-duplicate rather than storing it. Contributions carry
`doc_type` and a `vault_path` shaped by convention. `assemble_embedding_text`
decides what a note means to search from title, aliases, tags, summary and body.
Proposals are revision-bound and adjudicated by a separate scope. These are
answers to the problem of many agents writing into one corpus without
coordination.

A human's own notes do not obviously fit any of it, and the browse console
(ADR 0039) makes the mismatch visible rather than creating it. Once a person can
read the corpus comfortably in a browser, the notes that are *not* in it become
conspicuous: the operator reads agent notes in a good surface and keeps their
own somewhere else, and inline proposing sharpens the asymmetry, because they
will be improving an agent's prose in a better editor than their own writing
gets.

## The question, stated

Should human-authored notes live in the same corpus, and if so, under what
governance?

Neither answer is obviously right, which is why this is deferred rather than
decided.

**If they come in**, several settled decisions need re-examination rather than
assumption:

- *The dedup gate.* Flagging is right for an agent that may re-derive a note
  someone already wrote. A person writing deliberately about a topic the corpus
  covers is not making that mistake, and being refused would read as the tool
  arguing with them.
- *`doc_type` and `vault_path`.* Whether human notes are a kind, a facet, or
  indistinguishable — and what that implies for search, for compile, and for
  the read policy that already withholds `flagged` from agents.
- *Whether an agent should read them at all.* A human's rough notes are
  untrusted input in exactly the sense ADR 0021 means: text an agent reads and
  may be steered by. That is not an argument against inclusion, but it is an
  argument for deciding it explicitly.
- *Who reviews them.* A human's own note going through a proposal queue the
  same human decides is separation on paper only, the same objection ADR 0039
  raises against the reviewer counter-proposal.

**If they stay out**, the obligation is honesty rather than silence: the browse
console should say plainly which corpus it is showing, so "I could not find it
in the vault" never gets mistaken for "it was not written down." A surface that
presents a partial corpus as the whole one is worse than an export chore,
because the chore is at least visible.

## Why it is deferred

The browse console does not depend on the answer. It reads what is there and
proposes changes to it, and neither behaviour changes if human notes arrive
later. Deciding this first would block a surface that is useful now on a
question that is genuinely hard.

What must not happen is deciding it by accident — shipping the console, finding
it convenient, and importing hand-written material through a path whose dedup
and review semantics were designed for a different author. That is the drift
this record exists to prevent.

## Revisit when

Any of: the browse console is in real use and the missing notes are felt; a
concrete import is proposed; or the dedup gate refuses something a person wrote
deliberately. The first of those is the likely trigger, and it is a reason to
re-read this before acting rather than a signal that the answer has become
obvious.
