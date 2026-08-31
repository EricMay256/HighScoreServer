# 0038. A first-party reviewer authorization

Date: 2026-08-30

## Status

**Proposed.** Not implemented. Written so the decision can be taken on its
merits rather than under the pressure of the friction that prompted it, which
ADR 0037's session persistence has already reduced by roughly thirty-fold.

## Context

`vault:review` cannot be requested. `OAUTH_BASELINE_SCOPES` caps what a client
may ask for at read, write, and propose, and `/authorize` intersects the
request with that cap; the privileged scopes in
`OAUTH_OPERATOR_ENTITLEMENT_SCOPES` arrive only through
`issue_vault_credential grant-oauth`, keyed to a `family_id`.

The runbook is explicit that this is deliberate, and about *why* the weaker
version was rejected: review is "unreachable through this path by
construction, not by an operator declining on a screen." The corpus is
untrusted input read by agents, and ADR 0021's defence against injected
instructions is that a privileged capability is absent from the surface the
text can name.

The cost is a manual step. After authorizing the review console, the operator
runs a shell command before the console can do anything, and repeats it
whenever the OAuth family is replaced. Persisting the session (ADR 0037) moves
that from every tab close to roughly every thirty days, which is the refresh
token's lifetime. This ADR asks whether the remaining step should go too.

## Proposal

Add a distinct endpoint — `/vault/review/authorize` — where the granted scope
is a property of **the endpoint the operator visited**, not of a parameter the
client supplied. The operator authenticates exactly as now (password or Google,
against the configured allowlist), the consent screen states in terms that this
grants the power to apply and to delete, and the credential is minted with
`vault:read vault:review`.

The invariant that matters is preserved: no client can request `vault:review`
anywhere, and `/authorize` keeps its cap untouched.

## What would have to hold

1. **The redirect target must be first-party.** The endpoint must refuse any
   `redirect_uri` that is not the console's own path on this origin. With that,
   an operator tricked into completing the flow authorizes a reviewer session
   *in their own browser* and the attacker receives nothing. Without it, this
   is a way to hand `vault:review` to a third party, which is strictly worse
   than the problem it solves.

2. **Separation of duties must move into code.** The rule that `vault:review`
   is granted only to a family holding `vault:read` alone is enforced today by
   `issue_vault_credential`. The endpoint must enforce the same, or it becomes
   a path to `read+write+propose+review` on one credential.

3. **The audit trail must not thin out.** `grant-oauth` writes
   `vault.oauth.entitlement.grant`. A consent-time grant must write an
   equivalent, or privileged grants stop being visible where an operator looks
   for them.

4. **The documentation must move with it.** "Unreachable through this path by
   construction" would become false. A stale security claim is worse than the
   friction this removes, so the runbook and ADR 0021's discussion change in
   the same commit or the change does not land.

## Consequences if accepted

A consent screen capable of granting review begins to exist. Today the defence
is categorical — no screen can offer it. Afterwards it is conditional on that
endpoint being reachable only under the right circumstances, which is a weaker
class of guarantee even when every condition above holds. That is the real cost,
and it is not paid down by any amount of care in the implementation.

The blast radius is the deletion path. `vault:review` is the scope that lets a
near-duplicate case be rejected, which deletes the candidate note. A defect
that mis-scopes a credential here hands that to whoever holds it.

## Alternatives

**Do nothing (current state).** One shell command roughly monthly, on a
credential that is by design single-purpose and long-lived. The console already
renders the exact command with its own credential id parsed from its token, so
the step is copy-paste rather than research.

**Make the grant easier without moving the boundary.** A narrower
`grant-reviewer` subcommand, or accepting the credential id from the clipboard,
reduces the same friction without creating a screen that can grant review.
Cheaper, and it leaves the categorical guarantee intact.

## Recommendation

Defer. The friction that motivated this was measured against a console that
lost its session on every tab close; that is fixed. Revisit only if the monthly
step proves genuinely costly in practice, and prefer the narrower alternative
above if the goal is convenience rather than removing the operator from the
loop entirely.
