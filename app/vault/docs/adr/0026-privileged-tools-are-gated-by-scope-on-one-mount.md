# 26. Privileged tools live on the one mount, gated by the credential's scopes

Date: 2026-08-22

## Status

Accepted 2026-08-22.

Supersedes nothing, but **reverses the destination** ADR 0019's amendment and ADR 0023 both
assumed. Each said the privileged verbs "belong on the admin MCP surface"; neither had decided
what that surface was, and the answer is that it does not need to exist.

Depends on ADR 0021 (the MCP adapter and its scope-shaped tool surface), ADR 0019 and its
amendment (retirement is deletion; the review surface is REST-only), and ADR 0023 (promotion
candidacy is `vault:review`-gated).

## Context

Two verbs were finished except for a transport:

- **The review decision.** `VaultReviewService.decide`, shipped as three REST routes in v64.
- **Setting `promotion_status`.** `VaultPromotionService.set_promotion_status`, built and
  tested under ADR 0023, with no caller at all.

Both are gated on `vault:review`. The open question was where their tools live, and the
default assumption — carried in two ADRs and in an operator's notes — was a **separate admin
MCP server**, on the reasoning that privileged tools should not be exposed to ordinary
consumers, that privilege expansion is an attack vector, and that prompt injection is the
threat model.

Those three purposes are real. The question this ADR settles is whether a second mount is
what serves them.

## Decision

**The privileged tools go on the existing mount, filtered by `list_tools` on the presented
credential's scopes. There is no second MCP application.**

`_TOOL_SCOPES` gains four entries; nothing else about the adapter changes.

| Tool | Scope | Note |
| ---- | ----- | ---- |
| `vault_list_review_cases` | `vault:review` | ids and reasons; **no note bodies** |
| `vault_read_review_case` | `vault:review` | serves `flagged` content — the untrusted step |
| `vault_decide_review_case` | `vault:review` | `accepted` publishes, `rejected` deletes |
| `vault_set_promotion_status` | `vault:review` | ADR 0023's outstanding verb |

### Why scope filtering is sufficient for what was actually feared

Taking the three purposes in turn:

- **Not exposing privileged tools to ordinary consumers.** `list_tools` already filters on the
  credential (ADR 0021), so a session holding `vault:read` and `vault:write` does not see these
  tools and cannot name them. That is the same mechanism that already hides
  `vault_retire_note`, and it is the mechanism ADR 0021 calls the injection boundary.
- **Privilege expansion as an attack vector.** A client cannot widen itself. OAuth's
  `valid_scopes` caps a self-registering client at `vault:read`, `vault:write`, and the inert
  `vault:propose` capability (ADR 0024 as amended by ADR 0028),
  so `vault:review` is unreachable by request; it arrives only when an operator runs
  `issue_vault_credential grant`. A second mount adds nothing here — the gate is the scope, not
  the URL.
- **Prompt injection.** The defence is the tool being absent from the surface injected text can
  name, which scope filtering provides exactly.

### The residual risk, which no arrangement of mounts removes

A reviewer must **read** the note they adjudicate. `flagged` is the least-vetted text in the
corpus — the content the read surface withholds from every other caller precisely because the
write path declined to endorse it — and the same session then holds a tool that deletes. One
session, untrusted text in context, destructive tool present.

**A separate mount would not have fixed this**, and it is worth being explicit because the
separate-mount proposal read as though it would. Adjudication requires both capabilities at
once, on whatever surface it happens. What bounds the risk is not the topology but the verbs:

- **`reject` deletes a note that was never endorsed.** A review candidate is always a
  brand-new note the contribute path flagged, and its substance is by construction already in
  the corpus — that is what the case *says*. Pre-existing notes appear only as JSON evidence.
  The blast radius of a coerced rejection is one duplicate that never joined the corpus.
- **`accept` publishes a flagged note.** The worst outcome is a duplicate entering search.
- **Promotion moves a file between two folders.** Nothing is destroyed either way.

That is a materially narrower verb set than `vault_retire_note`, which can delete an endorsed
note and which ADR 0021 declines to call a recoverable class of mistake.

### The operating rule that carries the rest

Scope filtering makes the tool surface a function of **which credential is in which client**,
so the configuration is the boundary. The rule that follows, and it belongs in the runbook
rather than only here:

> **A reviewing credential holds `vault:read` and `vault:review`, and nothing else.**

Not `vault:delete`, not `vault:update`. Then the adjudicating session can publish, reject a
candidate, and set candidacy — and still cannot retire an endorsed note or overwrite one,
because those tools are absent from *that* session for the same reason the review tools are
absent from an ordinary one. That recovers most of what a separate mount was reaching for,
through the mechanism that already exists.

`claude-1`, the only live credential, holds `vault:read vault:write` — so production is
already in this shape, and a reviewer is a second credential rather than a widening of the
first.

## Consequences

### The boundary is now configuration, and that is the cost

A separate mount would have made the consumer surface *structurally* incapable of
destruction — a property of the code, surviving any credential mistake. This makes it
conditional on the operator not granting `vault:review` (or `vault:delete`) to an everyday
credential. Grant it once for convenience and every session that credential opens carries the
privileged tools, including sessions that are only searching.

That is a real reduction in defence depth, accepted deliberately: it costs one operating rule
and saves a second application, a per-application tool registry, a third configuration
variable, and a second URL to register. The rule is enforceable by looking at
`issue_vault_credential list`, which is a thing an operator already does.

### Nothing becomes per-application

`_TOOL_SCOPES` stays one registry, which was the largest piece of work the separate-mount
proposal implied. Had both applications shared it, a `vault:review` credential would have seen
the admin tools on the ordinary mount as well — the arrangement that proposal existed to
prevent, reintroduced by the shortcut.

### The REST routes stay

The three review routes shipped in v64 are not removed. They are what works with `curl`, they
are what an operator uses when they want no agent involved, and ADR 0019's amendment chose
them deliberately. This adds a second adapter over one service, which is ADR 0021's shape.

### `vault_list_review_cases` returns no bodies

Triage — "how big is the queue" — happens without pulling untrusted text into context at all.
Only reading a specific case does that, which makes the dangerous step explicit rather than
incidental to looking at a list.

### ADR 0023 stops being partially implemented

`promotion_status` becomes reachable, and `Agent/Promotion Candidates/` starts receiving
files.

## Alternatives considered

**A separate admin MCP mount** (`/api/v1/vault/admin/mcp/`), off by default, absent from OAuth
discovery. The proposal this ADR replaces. Its strongest form moved `vault_retire_note` there
too, which would have made the consumer mount append-only *structurally* rather than by
configuration — the one property scope filtering genuinely cannot express. Rejected on cost:
it needs a per-application tool registry, a third config variable, a second registration, and
it leaves the residual risk above completely untouched. The operating rule under "Decision"
buys most of the same protection for none of that.

Worth re-opening if either becomes true: a second person gains a credential, so "which
credential is in which client" stops being one operator's knowledge; or an agent starts
adjudicating unattended, where a coerced decision is not seen by a human at the time.

**No MCP at all; extend the REST surface with promotion.** The cheapest option, and the one
that removes the injection risk entirely because a human types the decision. Rejected because
adjudicating a queue by `curl` is why queues do not get adjudicated — the review flow shipped
in v64 and has processed nothing. Still the fallback if the queue stays empty in practice.
