# AGENTS.md — vault bounded context

Standing context for work under `app/vault/` and `vault_migrations/`. This file travels with
the package; the host repository's `AGENTS.md` governs everything outside these directories.

## What this is

The knowledge-platform bounded context: its own API models, domain records, Core tables,
repositories, services, auth, embeddings, HTTP routes, and MCP adapter. It holds runtime code
and schema definitions only — never corpus content, credentials, exports, or vectors.

It is currently hosted inside HighScoreServer, which is a staging location rather than
permanent ownership. See `docs/vault-extraction-manifest.md` for what leaves and what has to
be edited when it does.

## Persistence

- **SQLAlchemy 2.x Core, never the ORM.** No `DeclarativeBase`, `Session`, `relationship`,
  `mapped_column`, or `sessionmaker`. The host repo's raw-SQL ADR does not apply here; see
  `docs/adr/0001-sqlalchemy-core-for-vault-bounded-context.md`.
- Pydantic API models, domain records, Core tables, repositories, and services stay separate
  layers. Public request/response models are deliberately not database records.
- Repositories take an `AsyncConnection` as their first parameter and never acquire one
  themselves. Services own transactions.
- Core metadata exists for query construction and schema-drift tests. It never replaces
  Alembic and must never reach `create_all()` in production.
- Migrations are explicit reviewed SQL in `vault_migrations/`, a lineage separate from the
  host's, versioned in `vault.vault_alembic_version`.
- `pgvector` is used only here and leaves with the package.

## Isolation

- No imports between vault code and host leaderboard routes, models, repositories, or
  tables, in either direction. `tests/vault/test_boundaries.py` enforces this.
- Intra-package imports are **relative** (`from .constants import ...`). `app/vault/` must
  contain no `from app.` or `import app.` — that is what keeps extraction a directory move
  instead of a find-and-replace, and it is also asserted by `test_boundaries.py`.
- No cross-schema foreign keys, views, triggers, shared sequences, or cross-domain
  transactions. Everything lives in the qualified `vault` schema.
- Documentation and ADRs live in `app/vault/docs/`, never in the host's `docs/`. The ADR
  lineage here is independent and starts at 0001.

## Schema invariants

- **Audit events carry correlation identifiers, not foreign keys.** `vault_audit_events` has
  no FK to `vault_write_requests`: an audit insert must never fail on a referential
  constraint, events for rejected or unauthenticated writes must keep their idempotency key,
  and write requests must remain prunable. `target_type`/`target_id` follow the same rule.
  `latency_ms` is nullable because lifecycle events have no meaningful latency. See ADR 0002.
- **Embeddings live in `vault_document_embeddings`, keyed by `(document_id, profile_id)`** —
  not as columns on `vault_documents`. "Not embedded" is the absence of a row. Re-embedding a
  profile is an upsert. See ADR 0003.
- **`kind` is lifecycle; `doc_type` is taxonomy.** `document_kind_enum('note','wiki')` stays a
  coarse storage and lifecycle discriminator and keeps its role in
  `vault_documents_compile_provenance_consistent`. The governance Type Dictionary value lives in
  a separate nullable `doc_type TEXT`. The database CHECK constrains **shape only** — non-blank,
  ≤64 characters, printable, interior spaces allowed for names like "Agent Note" and "Wiki Page". Which names
  are legal is `types.yml`'s business, enforced in application code at the write boundary, so
  that adding a type stays a data change rather than a migration. Do not "tighten" that CHECK
  into a vocabulary list and do not widen the `kind` enum. Null means untyped, which is a real
  state. See ADR 0009.
- **`vault_path` is the only policy key.** `vault_documents.vault_path` is the vault-root-relative
  posix path with extension, `NOT NULL UNIQUE`, byte-identical to the governance scanner's
  `rel_path`. There is deliberately **no** `policy_scope` column: a document's context is a fold
  over every matching `folders.yml` rule, not one winning rule, and five of the eight fields
  overlay unconditionally while `default_type`/`allowed_types`/`purpose` fall back to a
  less-specific ancestor. Port `vault_governance.inheritance.resolve_context`; do not
  reimplement prefix matching by eye. Every glob is a literal prefix plus `/**`, which is why
  the `text_pattern_ops` index exists. See ADR 0010.
- **`status` and `doc_status` are different things.** `status` is the vault's own visibility
  state and the thing `routes.READABLE_STATUSES` gates on — a closed enum on purpose.
  `doc_status` is the `types.yml` Status Map value (`Evergreen`, `Stub`, `Proposed`), free text
  with a shape-only CHECK, validated per-type at the write boundary. Neither derives from the
  other. Do not gate reads on `doc_status`; that would move a security boundary into a file that
  changes without review. See ADR 0011.
- **`source_sha256` NULL means "no upstream file".** It is the SHA-256 of the Markdown a row
  replicates, and NULL says the row was authored in the database instead — which is how a
  mark-and-sweep run knows not to delete it. Reconciliation is mark-and-sweep keyed on
  `vault_path`, **scoped by path prefix**, sweeping only after a complete walk and refusing an
  implausibly small one. An unscoped sweep deletes every agent note. See ADR 0012.
- **The embedding text is title + aliases + tags + summary + body, and nothing else.** Timestamps
  and identifiers churn without changing meaning; `Type`/`Status` are excluded because they are
  columns (`doc_type`, `doc_status`) and filtering exactly beats matching fuzzily. `frontmatter`
  JSONB exists for faithful projection and is deliberately **not** embedded. Re-import and
  re-embed are separate: `vault_document_embeddings.embedded_text_sha256` decides the second, and
  NULL there means stale, not current. See ADR 0013.
- **`search_vector` uses `vault.text_array_to_string`, and must.** `array_to_string` is STABLE, so
  PostgreSQL rejects it in a generated column; `array_to_tsvector` is IMMUTABLE but emits
  unstemmed lexemes that silently never match a stemmed query. Aliases are weighted 'A'; tags are
  deliberately absent, being already served exactly by their GIN index.
- **`ai_read` is enforced twice, and fails closed.** `read_policy.READABLE_PATH_PREFIXES`
  mirrors the `ai_read: allowed` rules in the private `folders.yml`. Excluded folders are
  never imported; both search arms, the fusion hydration step, and fetch-by-ID filter again
  anyway, because the two layers cover different failures. A path matching no prefix is
  unreadable — never add a fallback that serves the unclassified. Not configuration, for
  ADR 0008's reason. `get_by_id(readable_only=...)` defaults **off** because reconciliation
  must load an excluded row in order to delete it. Reading leaves no diff, so unlike
  `ai_write` this can never be a CI gate. See ADR 0014.
- **`VAULT_TEXT_SEARCH_CONFIG` is a migration-time choice.** It is compiled into the
  persisted `search_vector` generated column, so changing it later requires a table rewrite
  and a GIN reindex, not a restart. A startup assertion compares the environment against the
  expression actually stored in the catalog. Search queries must use the same configuration:
  `websearch_to_tsquery(:config, :query)`.
- Contribution policy for the write path is **not** implemented here during the read-only
  phase. `vault_contrib.core.decide()` and `vault_contrib.models.Policy` remain normative and
  will be ported verbatim with their tests at switchover. See ADR 0004.

## Retrieval and embeddings

- The embedding **port** (`embeddings.py`) names no vendor. Adapters live in their own modules
  (`embeddings_openai.py`), and `embedding_runtime.py` is the only place that maps a provider
  name to an adapter. Nothing may import an adapter from the port.
- `settings.py` deliberately does **not** validate that an adapter exists for the configured
  provider. That separation keeps the Alembic environment free of transport imports; the
  registry raises instead.
- The embedding provider is **optional at runtime**. Without a credential the vault serves
  lexical-only search and says so in the response. Do not turn a missing key back into a
  startup failure — CI depends on this.
- `vector_status` distinguishes `used` / `not_configured` / `failed`. Keep those three
  separate: a broken provider must never be reportable as a deliberate lexical-only
  deployment. `failed` also logs at ERROR; `not_configured` logs nothing per request.
- Never log the query text or an embedding exception's message — both can carry user content.
  Log the exception *type* instead. `tests/vault/test_search.py` asserts this.
- Search must pass the text search configuration as a bound parameter —
  `websearch_to_tsquery(:config, :query)` — never the database default, never interpolation.
- **Search returns `active` only; fetch-by-ID also resolves `archived`, never `flagged`** (ADR
  0008). `routes.READABLE_STATUSES` is the single statement of that rule. Archived content is retired
  but legitimate, so a `related_ids`/`source_ids` reference still resolves; `flagged` means the
  write path declined to endorse it, and the consumer is an agent that will not check the
  `status` field. `VaultDocumentRepository.get_by_id` stays **unfiltered by default** — review
  tooling must be able to load a flagged document precisely because it is flagged — so the
  restriction belongs at the calling surface, not in persistence.
- The lexical arm **disjoins** the parsed query's terms (ADR 0007), rewriting ` & ` to ` | ` in
  `websearch_to_tsquery`'s output. Quoted phrases keep their `<->` operator, and a query whose
  parsed form contains `!` stays conjunctive — disjoining a negation inverts it. Do not
  "simplify" this back to a plain `websearch_to_tsquery`, and do not re-lex the raw query
  string with `to_tsvector`; that silently drops phrase support.
- The first profile is `openai/text-embedding-3-small:1536` (ADR 0005); the two arms combine
  by Reciprocal Rank Fusion (ADR 0006, amended by 0007). Changing provider or model is a
  controlled re-embed, never a credentials-only config change.

## Authentication

- **Agents authenticate with `hssv1_<credential-id>_<secret>`**, verified against
  `vault_agent_credentials`; only `sha256(secret)` is stored. `VAULT_READ_API_KEY` is gone and
  there is no global on/off secret — a credential verifies or it does not. See ADR 0015.
- The token is split **from the right**: credential IDs may contain `_`, secrets are hex so the
  last `_` is unambiguously the separator. Do not "simplify" this to a left split.
- A lookup miss still runs a comparison against a dummy hash. Removing that lets response
  timing enumerate valid credential IDs.
- Plain SHA-256 is correct **here** because secrets are machine-generated with full entropy.
  Do not carry that reasoning to human-chosen passwords.
- `last_used_at` is written only on success — it means "last used", not "last attempted".
- `401` for a bad or inactive credential, `403` for a valid one missing a scope. Neither
  response says which check failed.
- Scopes are verbs. *What* a credential may read is ADR 0014's path policy, a property of the
  folder rather than of the credential.

## Rate limiting

- **Per authenticated principal, never per IP** — agents share egress addresses and a
  credential is what an operator can revoke. `app/vault/rate_limit.py` carries a token bucket
  because slowapi lives in the host package and importing it would breach the isolation rule.
- Limits mirror the integration spec's table; `LIMITS` is the single statement of them.
- **Buckets are per process**, so the real ceiling is the limit times the worker count. Do not
  describe this as a hard limit in operator docs, and do not "fix" it in-process — the fix is a
  shared backend, and it only matters across hosts.
- A bucket may be pruned **only** when elapsed time proves it would have refilled. Dropping a
  partly-drained bucket silently refunds requests already charged.

## Working agreements

- Type hints on every function signature; no module-level side effects.
- Any value interpolated into DDL or SQL is validated first — treat it as an injection
  surface even when it comes from a trusted operator.
- Flag anything needing an Alembic revision rather than only changing code.
- Flag new dependencies explicitly; anything added here must be listed in the extraction
  manifest as leaving with the package.
- Material architectural decisions get a Nygard-format ADR in `docs/adr/`, continuing this
  lineage's own numbering.
