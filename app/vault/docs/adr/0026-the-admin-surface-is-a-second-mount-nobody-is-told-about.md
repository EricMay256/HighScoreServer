# 26. The admin surface is a second mount nobody is told about

Date: 2026-08-22

## Status

**Proposed.** This is the decision NEXT-STEPS item 6 is blocked on. Nothing below is built.

Depends on ADR 0021 (the MCP adapter and its scope-shaped tool surface), ADR 0019 and its
amendment (retirement is deletion; the review surface is REST-only), and ADR 0023 (promotion
candidacy is `vault:review`-gated and its tool "belongs on the admin MCP surface").

## Context

Two verbs are finished except for a transport, and both are waiting here:

- **The review decision.** `VaultReviewService.decide` exists and is exercised by three REST
  routes shipped in release v64.
- **Setting `promotion_status`.** `VaultPromotionService.set_promotion_status` exists and is
  tested (ADR 0023, implemented 2026-08-21). It has no caller at all.

Both are gated on `vault:review`. ADR 0019's amendment put the review routes on REST
deliberately and said an admin MCP "is where these belong if they ever move"; ADR 0023 said
the promotion verb "belongs on the **admin MCP surface**, not the general mount". Neither
said what that surface *is*, and that is the gap.

### The part that makes this non-obvious

ADR 0021's defence against instructions injected into note text is that **the tool is not
there**: a session authenticated with `vault:read` and `vault:write` has no
`vault_retire_note` to be talked into calling. That mechanism already exists and is already
scope-shaped — `list_tools` filters on the presented credential, and each tool re-checks.

So a naive reading says the work is already done: add the admin tools to `mcp.py`, gate them
on `vault:review`, and a credential without that scope never sees them. **That reading is
what this ADR exists to reject**, and the reason is worth stating carefully, because "scope
filtering already handles it" is true of the *ordinary* agent and false of the reviewer.

The reviewer's session is the problem. Adjudicating a flagged note means **reading it** —
`flagged` is the least-vetted text in the corpus, the content the read surface withholds from
every other caller precisely because the write path declined to endorse it — and then
deciding, where `rejected` deletes. One session, holding a tool that pulls untrusted text
into context and a tool that destroys, is exactly the shape ADR 0021 warns about. Scope
filtering does not help: the scopes are genuine, and the injection spends them.

**No arrangement of mounts eliminates that.** A reviewer must read what they adjudicate. What
a separate surface changes is who can reach it and what an ordinary session can discover, not
what a review session can be talked into.

## Decision

**A second MCP application, mounted at `/api/v1/vault/admin/mcp/`, off by default behind
`VAULT_ADMIN_MCP_ENABLED`, absent from OAuth discovery, and reachable only with an
operator-issued `hssv1_` credential holding `vault:review`.**

`build_vault_mcp_app` already takes a path and builds per-application state, so a second
instance is an argument rather than a fork of the module.

Four properties, each doing work the existing mount cannot:

- **It does not exist unless an operator turned it on.** `VAULT_ENABLED` gates the vault;
  this gates the admin surface separately, defaulting off, the way `VAULT_PUBLIC_URL` gates
  the authorization server. A deployment that never adjudicates through an agent has no admin
  surface to attack.
- **OAuth cannot reach it.** ADR 0024 caps a self-registering client at `vault:read` and
  `vault:write`, so no web-authorized client can hold `vault:review` without an operator
  running `grant` by hand. The admin mount publishes no protected-resource metadata, so a
  spec-compliant client is never even told it is there.
- **It is a different URL in a different client.** One credential is one capability profile
  (ADR 0021), and the operator decides which credential goes in which client. A separate
  endpoint makes "the reviewing client" a thing that exists rather than a scope an existing
  client happens to hold.
- **It can carry different middleware later** — a tighter pre-auth bucket, an allowlist —
  without touching the surface every ordinary agent uses.

### Why this is tolerable despite the residual risk

The risk that survives is a reviewing session being talked into a decision by the very text it
is reviewing. That is real, and it is bounded by **what the verbs can do**, which is the
argument this ADR actually rests on:

- **`reject` deletes a note that was never endorsed.** ADR 0019 and `ReviewState`'s definition
  are explicit: a candidate is always a brand-new note the contribute path flagged, its
  substance is by construction already in the corpus — that is what the case *says* — and
  pre-existing notes appear only as JSON evidence. The blast radius of a coerced rejection is
  one duplicate that was never part of the corpus. That is not the recoverable-mistake
  argument ADR 0021 declines to make about `vault_retire_note`; it is a narrower verb.
- **`accept` publishes a flagged note.** The worst outcome is a duplicate entering search.
- **Promotion moves a file between two folders.** `candidate` and `promoted`/`retracted`
  differ by a `vault_path`, and nothing is destroyed either way.

**`vault_retire_note` must not appear on this surface.** It is the one verb that can delete
an endorsed note, it is already available on the ordinary mount to a credential holding
`vault:delete`, and putting it beside the tool that reads flagged text would recreate exactly
the pairing this ADR is trying to avoid.

### What goes on it

Four tools, all `vault:review`:

| Tool | Service | Note |
| ---- | ------- | ---- |
| `vault_list_review_cases` | `VaultReviewService.list_pending` | ids and reasons; no note bodies |
| `vault_read_review_case` | `VaultReviewService.get` | **serves flagged content** — the untrusted step |
| `vault_decide_review_case` | `VaultReviewService.decide` | `accepted` publishes, `rejected` deletes |
| `vault_set_promotion_status` | `VaultPromotionService.set_promotion_status` | ADR 0023's outstanding verb |

`vault_list_review_cases` deliberately returns no bodies, so triage — "how big is the queue"
— can happen without pulling untrusted text into context at all. Only reading a specific case
does that, which makes the dangerous step explicit rather than incidental.

### The REST routes stay

The three review routes shipped in v64 are not removed. They are the surface that works with
`curl`, they are what an operator uses when they do not want an agent involved, and ADR 0019's
amendment chose them on purpose. This adds a second adapter over the same service, which is
ADR 0021's whole shape.

## Consequences

- **`app/vault/mcp.py` needs the admin tools kept out of its default tool set**, not merely
  scope-gated. If both applications share one `_TOOL_SCOPES`, a credential holding
  `vault:review` would see the admin tools on the *ordinary* mount as well, which is the
  arrangement this ADR rejects. The tool registry has to become per-application.
- **A third thing to configure**, and a third way to get a deployment subtly wrong. The
  configuration runbook gains a section; the failure mode of forgetting it is that the admin
  tools do not exist, which is the safe direction.
- **Promotion becomes reachable**, so ADR 0023 stops being "done except the verb" and
  `Agent/Promotion Candidates/` starts receiving files.
- **`prune_*`-style pruning is unaffected.** Nothing here writes a new table.

## Alternatives considered

**Admin tools on the existing mount, gated by `vault:review`.** The cheapest option, and the
one the existing mechanism already supports. Rejected because it makes the reviewing
credential a strictly-more-powerful version of the ordinary one on the same endpoint, so any
client configured with it gets the destructive tools in every session, including sessions that
are only searching. The separation this ADR wants is between *clients*, and a scope on a
shared endpoint cannot express it.

**No admin MCP; extend the REST surface with promotion.** Genuinely defensible, and the
cheapest of all — one route, no new decision, and the injection risk disappears because a
human types the decision. Rejected because adjudicating a queue by `curl` is the reason the
queue does not get adjudicated; the review flow shipped in v64 and has processed nothing. But
if the queue stays empty in practice, this is the option to fall back to, and nothing here
forecloses it.

**A separately deployed admin application.** The strongest isolation and the wrong cost. It
would need its own dyno, its own connection budget against a 20-connection plan that is
already allocated to one under the reserve, and its own deploy. The properties that matter —
off by default, undiscoverable, separately credentialed — are all available from a mount.

## What this does not decide

Whether a reviewing agent should be permitted to decide *at all*, as opposed to summarising a
case and leaving the verdict to a human. That is a policy question about how the operator
wants to work, not a structural one, and it can be answered later by simply not granting
`vault:review` to an agent — the surface is the same either way.
