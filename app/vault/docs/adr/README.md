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
| 0019 | [Retiring a document deletes it, and the ledger outlives it](0019-retiring-a-document-deletes-it.md) | Accepted (amended 2026-08-21: a settled review case releases its candidate) |
| 0020 | [`vault:write` narrows to contribute; update and delete are their own scopes](0020-write-scopes-split-by-verb.md) | Accepted |
| 0021 | [MCP is a second adapter, and a credential's scopes shape its tool surface](0021-mcp-is-a-second-adapter-with-scope-shaped-tools.md) | Accepted (its OAuth deferral is superseded by 0024) |
| 0022 | [Two trees, one writer each: humans author markdown, agents author through the service](0022-two-trees-one-writer-each.md) | Accepted (amended 2026-08-20: `vault_path`'s leaf name is the title slug) |
| 0023 | [Candidacy is a field, and the export projects it into a folder](0023-the-export-projects-only-the-engine-managed-folders.md) | Accepted |
| 0024 | [The vault runs its own authorization server, and an OAuth token is a credential row](0024-oauth-tokens-are-credentials-minted-at-authorization.md) | Accepted |
| 0025 | [The vault stores edges and does not traverse them](0025-the-vault-stores-edges-and-does-not-traverse-them.md) | Accepted (amended 2026-08-26: both boundaries built; a Wiki Page's `Related` is one) |
| 0026 | [Privileged tools live on the one mount, gated by the credential's scopes](0026-privileged-tools-are-gated-by-scope-on-one-mount.md) | Accepted (reverses the separate admin MCP that 0019 and 0023 assumed) |
| 0027 | [The service plans a compile run; the agent writes it](0027-the-service-plans-a-compile-run-and-the-agent-writes-it.md) | Accepted (amends 0016's "no dedup, no write" for wiki pages) |
| 0028 | [Amendments are revision-bound proposals, not notes](0028-amendments-are-revision-bound-proposals.md) | Accepted (amended 2026-08-25: bounded general body diffs and removal acknowledgement) |
| 0029 | [Operator-granted OAuth entitlements belong to the refresh family](0029-oauth-entitlements-belong-to-the-refresh-family.md) | Accepted |
| 0030 | [An edge value is checked for shape, and still never for existence](0030-edge-values-are-shape-checked-never-existence-checked.md) | Accepted (answers 0025's 2026-08-26 amendment) |
| 0031 | [Search names candidates; fetch returns documents](0031-search-is-a-discovery-surface.md) | Accepted (amends 0006, which ordered results without saying what one is) |
| 0032 | [A contribution reports its verdict; the gate's working is opt-in](0032-a-contribution-reports-its-outcome.md) | Accepted (refines 0016) |
| 0033 | [An amendment may name a span; the server writes the diff](0033-an-amendment-may-name-a-span.md) | Accepted (adds a third authoring form to 0028) |
| 0034 | [Embeddings are document-level, and chunking is deferred against a measured trigger](0034-embeddings-are-document-level-and-chunking-is-deferred.md) | Accepted |
| 0035 | [A contributor may describe its own recent note](0035-a-contributor-may-describe-its-own-recent-note.md) | Accepted (amends 0028; refines 0032) |
| 0036 | [Metadata is a change kind of its own](0036-metadata-is-a-change-kind-of-its-own.md) | Accepted (adds a fourth authoring form to 0028) |
| 0037 | [The review console is an OAuth client, not an operator session](0037-the-review-console-is-an-oauth-client.md) | Accepted |
| 0038 | [A first-party reviewer authorization](0038-a-first-party-reviewer-authorization.md) | Proposed (deferred; recommends the narrower alternative) |
| 0039 | [A browse-and-propose console, separate from the reviewer](0039-a-browse-and-propose-console.md) | Accepted (reviewer-side editing deferred) |
| 0040 | [An authorization carries an operator-assigned label](0040-an-authorization-carries-an-operator-label.md) | Accepted (preserves 0024's amendment) |
| 0041 | [Human-authored notes in the vault](0041-human-authored-notes-in-the-vault.md) | Deferred |
| 0042 | [A mutable state store beside the corpus](0042-a-mutable-state-store-beside-the-corpus.md) | Considered, not scheduled |

## Reserved numbers

**Claim a number here before writing an ADR on a branch.** On 2026-08-26 four branches ran in
parallel against this lineage; 0030 was claimed for metadata-only search on one and merged as
edge-value validation from another, and the loser renumbered. The three that followed the
convention (0033, 0034, 0035) landed without collision, and the only merge conflicts were
adjacent rows in the table above.

A reservation is not a decision. If the work is abandoned, delete the row and let the number be
reused — an unexplained gap in the lineage is worse than a reused number.

The table is empty because nothing is currently in flight. That is a state, not an invitation to
remove the section.

| ADR | Claimed for | Branch |
| --- | ----------- | ------ |
