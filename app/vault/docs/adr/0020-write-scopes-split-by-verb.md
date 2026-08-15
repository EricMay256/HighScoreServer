# 20. `vault:write` narrows to contribute; update and delete are their own scopes

Date: 2026-08-15

## Status

Accepted.

## Context

ADR 0015 established operator-issued agent credentials carrying scopes, and set
the rule that **scopes are verbs** — what a credential may *do*, as distinct
from what it may reach, which ADR 0014 makes a property of the path rather than
of the credential.

The write surface then grew past the one verb that existed. ADR 0018 added
replacement (`PUT /notes/{id}`) and ADR 0019 added retirement
(`DELETE /notes/{id}`), and both were gated on `vault:write`, because that was
the scope that existed and adding one looked like ceremony at the time.

The result is that a single grant means three things, one of which is
destructive and irreversible. ADR 0019 is explicit that retirement is a
**delete** — no archived row, no tombstone, nothing a caller can still resolve —
so `vault:write` is in practice "may permanently destroy any note in the
corpus". Every credential issued to date is an `importer`, whose actual need is
contribute and replace and never delete.

The system already contained the judgement that deletion is the dangerous verb.
`LIMITS` gives `retire` the tightest bucket in the table — 10/min burst 5,
against 30/min burst 20 for contribute — on the stated reasoning that "a loop
that deletes is worse than a loop that writes". That reasoning is correct and it
was expressed in the wrong layer: a rate limit bounds how *fast* a principal may
destroy things, not *whether* it may. A quota is not an authorization boundary.

## Decision

Split the write verb three ways, matching the three routes:

| Scope | Route | Operation |
| --- | --- | --- |
| `vault:write` | `POST /contributions` | Contribute a new note |
| `vault:update` | `PUT /notes/{id}` | Replace a note's content |
| `vault:delete` | `DELETE /notes/{id}` | Retire a note, destroying it |

`vault:write` **narrows** rather than remaining a superset. A grant that keeps
implying the other two would leave the permissive default in place and make the
new scopes decorative: the only credential shape anyone would issue is the one
that already works.

**The scope is `vault:delete`, not `vault:retire`,** though the route handler,
the service class and the quota bucket all say retire. ADR 0019's whole point is
that retiring *is* deletion; the audience for a scope name is an operator
deciding whether to hand it to a client, and "retire" reads as reversible to
someone who has not read ADR 0019. The internal vocabulary keeps "retire"
because that is the operation; the permission says what granting it risks.

**Migration `0007_write_scope_split` changes the schema only.** It widens the
`vault_agent_credentials_scopes_known` CHECK constraint and grants nothing.

Widening the credentials that already exist is a **manual, per-credential,
one-time** operation, and deliberately not idempotent-by-rerun. The first draft
of this decision put that grant in the migration so pre-split clients kept
working, which is a reasonable one-time intent expressed in the one place that
guarantees it is not one-time: a migration is a procedure that reruns.
Rebuilding a staging database, testing a revision, or rolling back and
redeploying would each silently restore permissions an operator had deliberately
removed, with nothing in the logs saying a privilege had been granted.

The threat model here is not primarily an attacker. Anyone who can run Alembic
against a database already holds `DATABASE_URL` or deploy access, and can
`UPDATE` the scopes column directly — the migration hands them nothing. The
likely failure is an ordinary operator doing ordinary maintenance and silently
undoing a tightening they made on purpose. That is worse than the attacker case
because it needs no attacker.

`downgrade()` still strips `vault:update` and `vault:delete`, because the
narrowed constraint rejects rows carrying them and the `ALTER` would otherwise
fail partway through. That is constraint satisfaction rather than an
authorization decision, and the resulting asymmetry is the safe direction: a
down-then-up cycle can only ever *reduce* what a credential may do.

## Consequences

Least privilege becomes *expressible*, which it was not before, and applies to
credentials issued from here on. An importer-shaped credential — contribute and
replace, never delete — is now a thing an operator can issue, and it is the
shape the only real client should have had all along.

**Deploying this without granting anything breaks pre-split clients**, and that
is now an explicit operator step rather than an automatic one. A credential
issued before the split holds `vault:write` alone, so its replace and retire
calls begin returning `403` — a real failure, and the price of not having a
migration that re-grants. The mitigation is that the failure is loud, one-time,
and fixed either by a deliberate per-credential `UPDATE` or, better, by
reissuing with exactly the scopes that client needs.

The cost is small here in a way it might not be elsewhere: at the time of the
split, every credential in existence was a local `importer` on one developer
machine, and production had never deployed the vault and held no vault
credentials at all. A deployment with many live third-party credentials would
weigh this differently, and should — the argument is about *where* the grant
happens, not that grants are wrong.

Granting all three scopes reproduces the old `vault:write` exactly. That is
fine: it is now a decision made at issuance rather than a default nobody chose.

**The migration is not reversible in the authorization sense.** Its downgrade
strips `vault:update` and `vault:delete`, because the old vocabulary cannot
express them — so a credential issued with `vault:delete` and no `vault:write`
loses the capability entirely rather than being mapped onto something. The
downgrade is a schema rollback, and the honest failure mode is a credential that
stops working rather than one that silently retains a permission the schema no
longer knows about.

`vault:review`, `vault:compile` and `vault:export` remain recognised and granted
by no route, unchanged by this. The pattern this ADR sets — a verb per
route, decided when the route is built — is what those should follow rather than
being folded into an existing grant for convenience, which is exactly how
`vault:write` came to mean three things.
