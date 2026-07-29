# Vault Architecture Decision Records

This lineage is owned by the vault bounded context, is numbered independently of the host
repository's `docs/adr/`, and moves with `app/vault/` when the package is extracted.

| ADR | Title | Status |
| --- | ----- | ------ |
| 0001 | [SQLAlchemy Core for the vault bounded context](0001-sqlalchemy-core-for-vault-bounded-context.md) | Accepted |
| 0002 | [Audit events carry correlation identifiers, not foreign keys](0002-audit-events-carry-correlation-identifiers.md) | Accepted |
| 0003 | [Embeddings in a join table keyed by profile](0003-embeddings-in-a-join-table-keyed-by-profile.md) | Accepted |
| 0004 | [Stage A `core.decide()` is normative for the write path](0004-stage-a-core-decide-is-normative.md) | Accepted |
| 0005 | [OpenAI text-embedding-3-small as the first embedding profile](0005-openai-text-embedding-3-small-as-the-first-profile.md) | Accepted |
| 0006 | [Hybrid retrieval fused by reciprocal rank](0006-hybrid-retrieval-fused-by-reciprocal-rank.md) | Accepted |
