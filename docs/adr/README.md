# Architecture Decision Records

This directory contains the architectural decisions made on HighScoreServer, recorded in the [Nygard ADR format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions). Each record captures the context that produced a decision, the decision itself, and its consequences. ADRs are immutable: when a decision is reversed or refined, a new ADR is written that supersedes the old one rather than editing history.

ADRs 0001–0006 were recorded retroactively on 2026-04-12 as part of formalizing the project's decision log; the decisions themselves predate the records. ADR 0007 was written at the time of the decision.

| #    | Title                                                                                  | Status   |
|------|----------------------------------------------------------------------------------------|----------|
| 0001 | [Guest accounts over nullable foreign keys](0001-guest-accounts-over-nullable-foreign-keys.md) | Accepted |
| 0002 | [Raw SQL over ORM](0002-raw-sql-over-orm.md)                                           | Accepted |
| 0003 | [Period bucketing via upsert](0003-period-bucketing-via-upsert.md)                     | Accepted |
| 0004 | [Ascending and descending sort order as a first-class concept](0004-ascending-and-descending-sort-order.md) | Accepted |
| 0005 | [Sync over async](0005-sync-over-async.md)                                             | Accepted |
| 0006 | [JWT plus opaque refresh tokens](0006-jwt-plus-opaque-refresh-tokens.md)               | Accepted |
| 0007 | [In-process cache over Redis at single-dyno scale](0007-in-process-cache-over-redis.md) | Accepted |
