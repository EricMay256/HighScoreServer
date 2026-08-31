# 0040. An authorization carries an operator-assigned label

Date: 2026-08-30

## Status

**Proposed.** Not implemented.

## Context

Principals read as `oauth-<uuid4>`. That is deliberate and should stay:
`principal_for_client` derives the id from the registration rather than the
client's declared name because the registration endpoint is open and the name
is unverified free text, so a name-derived principal collided across separately
approved clients — which then shared an idempotency namespace and a quota. ADR
0024's 2026-08-23 amendment records that.

The cost is that an operator looking at their own authorizations sees a list of
uuids. Deciding which family to entitle, which to revoke, or which the review
console is currently using means matching uuids by hand, and the console's own
header reads "credential 7f3a…" — technically exact and humanly useless. With
two consoles (ADR 0039) and separate families for importer, compiler and any
exporter, the number of indistinguishable uuids only grows.

`vault_agent_credentials.display_name` exists but does not solve this. It is
set at mint time from the client's declared name, it lives on the credential
rather than the authorization, and credentials are re-minted on every refresh —
so it is a copy of unverified client text, duplicated per rotation, and not
something an operator can change.

## Decision

Add a nullable `label` to `vault_oauth_grants`, set and changed by the operator,
carried as a display attribute of the authorization.

**The label is never identity.** It does not derive the principal id, key a
quota, key an idempotency namespace, appear in an audit record's principal
field, or resolve a credential. Those all stay on the id. The label answers
"which of these is my laptop's review console" and nothing else — which is
precisely the constraint ADR 0024's amendment was written to establish, kept by
separating the two concerns instead of choosing between them.

**On the family, not the credential.** The family is the durable thing an
operator reasons about: it is what an entitlement is keyed to, what a refresh
chain belongs to, and what survives rotation. A label on the credential would
be re-minted hourly and would have to be copied forward by code that has no
business knowing about names.

**Labels are not unique, and that is the point.** Enforcing uniqueness would
make the label a second identifier, which is the mistake this ADR is shaped to
avoid. Two authorizations may both be called "laptop"; the uuid distinguishes
them and always did.

**Operator-set, not self-declared.** A client naming itself is the unverified
free text problem again. It is harmless while the value is display-only, but
"display-only" is a property of today's code rather than a guarantee, and an
operator-set label needs no such assumption. `issue_vault_credential` grows a
subcommand to set and clear it.

## Consequences

`issue_vault_credential list` becomes readable, and the entitlement commands can
name what they are about to widen rather than echoing a uuid back. Both consoles
can show a name in place of a credential id.

The label is unverified operator text and reaches a browser, so it is rendered
as text and never as markup. Both consoles already build their DOM through
`textContent`, so this is a constraint to keep rather than one to add.

Nothing existing changes meaning: rows without a label behave exactly as now,
which is what makes this additive rather than a migration of identity.

An Alembic revision in the vault lineage adds the column. It is nullable with
no backfill — an unlabelled authorization is the ordinary state, not a
deficiency.

## Alternatives considered

**Reuse `display_name`.** It is already there and already shown. Rejected
because it is on the wrong object and has the wrong provenance: a per-credential
copy of a client's own claim about itself, refreshed hourly. Giving an operator
a rename that quietly reverts on the next rotation is worse than no rename.

**Make the principal id readable.** Slugify the name into the id and accept
collisions. That is the thing ADR 0024's amendment reversed, and reversing it
back would re-merge quota and idempotency across separately approved clients.

**A local mapping in each console.** Names in browser storage, never sent
anywhere. Cheap, and wrong at exactly the moment it matters: the operator
running `grant-oauth` in a shell is the one who needs to know which family
they are widening.
