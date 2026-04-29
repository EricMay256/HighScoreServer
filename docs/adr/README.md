# Architecture Decision Records

This directory contains the architectural decisions made on HighScoreServer, recorded in the [Nygard ADR format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions). Each record captures the context that produced a decision, the decision itself, and its consequences. ADRs are immutable: when a decision is reversed or refined, a new ADR is written that supersedes the old one rather than editing history.

ADRs 0001–0006 were recorded retroactively on 2026-04-12 as part of formalizing the project's decision log; the decisions themselves predate the records. ADR 0007 was written at the time of the decision.

## Conventions

**Status line is mutable.** The `## Status` line of an ADR may be updated when
the ADR's status changes — for example, from `Accepted` to `Superseded by
0008`. This is the only part of an ADR file that is edited in place after
commit; context, decision, and consequences are immutable.

**Bootstrap-phase editability.** ADRs 0001–0007 were committed as an initial
decision log for a solo-developer project and were left open for accuracy, 
clarity, and to absorb observations from subsequent audit phases while minimizing 
superseding ADRS. This is an explicit departure from strict Nygard conventions 
and exists because the ADRs have no external readers yet — the immutability 
rule primarily solves a team-coordination problem (preventing stale citations 
across reviewers), which doesn't apply while the project has a single author.

**Strict superseding begins with ADR 0008.** From ADR 0008 onward, the
immutability rule applies in full: new ADRs for reversals and refinements,
no in-place edits beyond the status line, and a commitment to treat the
decision log as an append-only record. `requires_auth` rename ADR will be 
0008 and is the first ADR written under the strict convention.

| #    | Title                                                                                  | Status   |
|------|----------------------------------------------------------------------------------------|----------|
| 0001 | [Guest accounts over nullable foreign keys](0001-guest-accounts-over-nullable-foreign-keys.md) | Superseded by 0008 |
| 0002 | [Raw SQL over ORM](0002-raw-sql-over-orm.md)                                           | Accepted |
| 0003 | [Period bucketing via upsert](0003-period-bucketing-via-upsert.md)                     | Accepted |
| 0004 | [Ascending and descending sort order as a first-class concept](0004-ascending-and-descending-sort-order.md) | Accepted |
| 0005 | [Sync over async](0005-sync-over-async.md)                                             | Accepted |
| 0006 | [JWT plus opaque refresh tokens](0006-jwt-plus-opaque-refresh-tokens.md)               | Accepted |
| 0007 | [In-process cache over Redis at single-dyno scale](0007-in-process-cache-over-redis.md) | Accepted |
| 0008 | [Naming Audit: requires_claimed_account and client-side enums](0008-naming-audit-requires-claimed-account-and-client-enums.md) | Accepted |
