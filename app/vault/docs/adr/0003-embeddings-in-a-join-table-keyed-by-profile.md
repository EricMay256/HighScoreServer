# 3. Embeddings in a join table keyed by profile

Date: 2026-07-28

## Status

Accepted

## Context

The first draft stored a document's vector on `vault_documents` itself, as three nullable
columns — `embedding vector(1536)`, `embedding_model`, `embedded_at` — held consistent by a
three-way check constraint requiring all three to be null or all three to be set.

That shape assumes a document has at most one embedding, forever. Several near-term needs
contradict it.

**Changing embedding provider or model requires holding two profiles at once.** Re-embedding
a corpus is not atomic. Comparing retrieval quality between the old and new profile, and
calibrating similarity thresholds against both, requires them to coexist. A single column
forces the migration to be a destructive cutover with no way to evaluate the replacement
before committing to it, and no way back if it is worse.

**Re-embedding writes churn on the wrong table.** `vault_documents` carries a persisted
generated `search_vector` column and its GIN index. Updating an embedding column on that table
makes every re-embed rewrite rows whose lexical index has not changed, and contends with
readers of document content.

**A nullable column makes "not yet embedded" a tri-state.** With columns, absence of an
embedding is a null that the schema must then keep consistent across three fields — hence the
check constraint. The constraint exists only because the representation invites incoherent
states.

The `embedding_model` column was also doing double duty: it identified the provider and model,
but the vector's meaning depends on provider, model, *and* dimension count together. That
composite is better named once, as a profile identifier, than reconstructed from parts.

## Decision

Embeddings move to `vault.vault_document_embeddings`, keyed by
`(document_id, profile_id)`, with `embedding` and `embedded_at` both `NOT NULL` and
`ON DELETE CASCADE` from `vault_documents`.

`profile_id` is a single opaque identifier naming provider, model, and dimensionality
together (for example `openai/text-embedding-3-small:1536`), constrained to
`^[A-Za-z0-9._:/-]{3,128}$`.

The `embedding`, `embedding_model`, and `embedded_at` columns are removed from
`vault_documents`, along with `vault_documents_embedding_consistent`. The consistency check
disappears rather than moving: with `NOT NULL` columns on a table whose rows exist only when
an embedding does, there is no incoherent state left to forbid.

Indexes are `idx_vault_document_embeddings_hnsw` using `hnsw (embedding vector_cosine_ops)`
with no partial predicate, and `idx_vault_document_embeddings_profile` on `profile_id`.

## Consequences

Two profiles can be populated simultaneously, so re-embedding becomes an additive background
job followed by a read-side switch, and threshold calibration can compare them directly.
Re-embed churn is confined to a narrow table and leaves `vault_documents` and its stored
generated column untouched. "Not embedded under this profile" is the absence of a row, which
needs no constraint to stay coherent, and re-embedding one profile is an idempotent upsert on
the primary key.

The significant cost is retrieval recall under a single unpartitioned HNSW index. Because the
index covers every profile's vectors, filtering to one profile is a **post-filter**: the index
returns nearest neighbours across all profiles and the profile predicate removes the ones that
do not match, which can leave fewer than the requested number of results. With exactly one
populated profile this is free. Once a second profile is populated, the remedy is a partial
HNSW index per profile (`WHERE profile_id = '<literal>'`), added by a migration at that time.

Baking such a literal into revision 0001 would be premature: there is one profile today and
its identity is not yet chosen, so the migration would encode a guess. The cost of deferring
is that the first genuine two-profile re-embed must be preceded by an index migration, which
is a known and scheduled step rather than a surprise.

A second cost is that reading a document with its vector is now a join, and writing both is
two statements. This is the intended direction — most read paths want the document without its
vector — but code that wants both must ask for both.
