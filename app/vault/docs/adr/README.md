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
| 0006 | [Hybrid retrieval fused by reciprocal rank](0006-hybrid-retrieval-fused-by-reciprocal-rank.md) | Accepted (amended by 0007) |
| 0007 | [The lexical arm disjoins query terms](0007-lexical-arm-disjoins-query-terms.md) | Accepted |
| 0008 | [The read surface resolves archived documents and withholds flagged ones](0008-read-surface-resolves-archived-withholds-flagged.md) | Accepted |
| 0009 | [`doc_type` is text validated against `types.yml`, not a second enum](0009-doc-type-is-text-validated-against-types-yml.md) | Accepted |
| 0010 | [`vault_path` is the only policy key; there is no resolved scope column](0010-vault-path-is-the-only-policy-key.md) | Accepted |
| 0011 | [`doc_status` carries the Status Map; `status` stays the visibility gate](0011-doc-status-carries-the-status-map.md) | Accepted |
| 0012 | [Markdown-authored layers reconcile by mark-and-sweep over a content hash](0012-markdown-layers-reconcile-by-mark-and-sweep.md) | Accepted |
| 0013 | [The embedding text carries semantic frontmatter, not bookkeeping](0013-embedding-text-is-semantic-fields-only.md) | Accepted |
| 0014 | [`ai_read` excludes at import and again at query time](0014-ai-read-excludes-at-import-and-at-query.md) | Accepted |
| 0015 | [Operator-issued agent credentials replace the shared read key](0015-agent-credentials-replace-the-shared-read-key.md) | Accepted |
| 0016 | [The governed write path](0016-the-governed-write-path.md) | Accepted (amended 2026-08-12: `flag_at` derivation) |
