# Vault Architecture Decision Records

This lineage is owned by the vault bounded context, is numbered independently of the host
repository's `docs/adr/`, and moves with `app/vault/` when the package is extracted.

**Numbers collide with the host lineage** — this 0009 is `doc_type` validation, the host's is
Alembic adoption, and 0008 and 0012–0015 overlap the same way. Inside `app/vault/`, a bare
"ADR 0016" means *this* lineage. Anywhere else, cite it as "vault ADR 0016".

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
| 0016 | [The governed write path](0016-the-governed-write-path.md) | Accepted (amended 2026-08-12: `flag_at` derivation; 2026-08-13 and 2026-08-16: idempotency digest) |
| 0017 | [Facets carry classification, outside the embedding text](0017-facets-carry-classification-outside-the-embedding.md) | Accepted |
| 0018 | [Updates are a distinct endpoint that refuses on collision](0018-updates-are-a-distinct-endpoint-that-refuses-on-collision.md) | Accepted |
| 0019 | [Retiring a document deletes it, and the ledger outlives it](0019-retiring-a-document-deletes-it.md) | Accepted |
| 0020 | [`vault:write` narrows to contribute; update and delete are their own scopes](0020-write-scopes-split-by-verb.md) | Accepted |
| 0021 | [MCP is a second adapter, and a credential's scopes shape its tool surface](0021-mcp-is-a-second-adapter-with-scope-shaped-tools.md) | Accepted |
| 0022 | [Two trees, one writer each: humans author markdown, agents author through the service](0022-two-trees-one-writer-each.md) | Accepted |
