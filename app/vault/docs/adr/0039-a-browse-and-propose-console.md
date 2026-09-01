# 0039. A browse-and-propose console, separate from the reviewer

Date: 2026-08-30

## Status

Accepted. The browse console, `GET /notes`, the REST span-edit kind, and inline
proposing are implemented. Reviewer-side editing under a second credential --
the last section of the decision below -- is not, and remains optional: the
rest stands without it.

## Context

The review console (ADR 0037) gave the vault a human surface for adjudicating
what agents propose. It does not give a human a way to *read* the corpus, and
it deliberately cannot give them a way to propose: `vault:review` may be granted
only to a family holding `vault:read` alone, so the reviewer credential cannot
also hold `vault:propose`.

That leaves two gaps.

Reading the vault as a human means an export chore — `export_vault_markdown.py`
into some other reader — which is enough friction that the corpus is mostly
read by agents and mostly written by them. A human who notices clumsy wording
while reading has nowhere to say so.

And a reviewer looking at an agent's proposal can accept it or reject it, but
cannot improve it. The realistic case is a proposal that is *nearly* right: the
change is correct and a sentence is badly phrased. Today that is a rejection
and a round trip through the agent, or an acceptance of prose nobody is happy
with.

## Decision

Add a second console at its own endpoint, holding `vault:read` and
`vault:propose`, for browsing the corpus and proposing changes inline. The
reviewer console keeps `vault:read` and `vault:review` and stays where it is.
Two surfaces, two credentials, one queue between them.

**The browse console needs no operator grant.** `vault:propose` is in
`OAUTH_BASELINE_SCOPES`, so it authorizes through the ordinary flow: sign in and
work. None of the entitlement machinery the reviewer needs applies to it. This
is worth stating plainly because it inverts the expected order — the surface
that *writes* is cheaper to authorize than the one that reads and decides,
because proposals are inert and decisions are not.

**Inline rewording is a span edit, not a diff.** `SpanEdit` already takes
`expected_text` and `replacement_text`, anchors on an exact match, and converts
to the canonical unified diff server-side, with occurrence and ambiguity
handling built. Selecting a sentence and offering a replacement is exactly that
operation. A browser that generated diffs itself would need a diff
implementation the page cannot fetch and should not hand-roll; sending the two
texts avoids the question entirely.

Two endpoints are missing and both are small:

- **Browse.** `/search` and `/notes/{id}` exist; there is no way to list. The
  repository already has `list_under_path_prefixes` with keyset paging, so this
  is an exposure rather than a design.
- **Span-edit proposals over REST.** The kind exists in the service and reaches
  only the MCP adapter. The HTTP surface accepts `replacement`, `body_diff` and
  `metadata`; it should accept a span edit too, converted by the same code.

**Editing a proposal is done as the proposer, not as the reviewer.** When a
reviewer wants to fix wording, the console submits the edit under a *second*
credential holding `vault:read` and `vault:propose` — the browse console's
credential — and the reviewer credential then decides the result. The
separation ADR 0021 draws is preserved rather than reasoned around: one
principal authored the change, another applied it, and the audit trail shows
both.

## Consequences

A page may hold two OAuth families at once. That is the real cost of this
decision and it should not be understated: a single token lifecycle produced
most of the defects found in the review console -- cross-tab rotation burning
the family, a persisted token nothing presented, a renewal that left an inert
page. Two lifecycles is not twice the surface but it is more than one, and the
sequencing between them (which credential is signed in, what happens when only
one expires) is new ground.

Mitigation is that the lifecycle is now solved once and reusable: the Web Lock,
the persisted record with its expiry, the settled resume, and the boot path that
can actually be driven by a test all carry over.

Editing an agent's proposal creates a *new* proposal rather than mutating the
original. That is forced by ADR 0028 -- proposals are immutable and
revision-bound -- and it is the right shape anyway: the reviewer's improved
wording is a claim to be decided, not an amendment applied in flight. The
original settles as rejected or stale, and the trail shows what the agent wrote
and what the human preferred.

## Alternatives considered

**Let `vault:review` propose.** The smallest change and the one that quietly
removes the guarantee. ADR 0021's defence is against instructions in note text
steering an agent, and a human retyping a sentence is not that threat -- but the
rule as written is what makes the reviewer credential analysable, and a
single-operator deployment that authors and applies its own edits has no
separation left to inspect.

**A reviewer counter-proposal kind.** A first-class "reject with replacement"
attributed to the reviewer. Rejected because in a one-operator deployment the
same person decides it, which collapses the separation in practice while
preserving it in the schema -- the worst of both, because it reads as separated.

**One console with both scope sets.** Impossible as scoped, and undesirable if
it were: the separation guard exists precisely so a credential that can apply
cannot also author.

## Deferred

Whether human-authored notes belong in the vault at all is a separate decision
and a larger one. See ADR 0041.
