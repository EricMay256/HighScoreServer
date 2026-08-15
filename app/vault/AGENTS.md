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
- **`facets` classifies; `tags` describes — and the difference is load-bearing.** `facets` JSONB
  (`{"project": ["hss"]}`) relates notes to each other and is **never** read by
  `assemble_embedding_text`. That exclusion is structural, not stylistic: measured on ten real
  documents, one shared tag raised *every* pairwise cosine, mean +0.0385 against a dedup margin
  of 0.0094 — over 4x. Putting classification in `tags` would lift the known-distinct floor above
  the known-duplicate ceiling and make `flag_at` uncalibratable. Do not "simplify" facets into
  namespaced tags, and do not add `facets` to the embeddable field set. Facet *names* are a
  closed set in `facets.py`; values are open, per ADR 0009's precedent. A facet must never gate
  a read — it is authored content, so ADR 0010 keeps `vault_path` the only policy key. See
  ADR 0017.
- **`related_ids` / `source_ids` are opaque and unvalidated on purpose.** A contribution may
  reference a note that is archived, flagged, or not yet written; a foreign key would fail the
  write for the reason ADR 0002 already rejected for audit events.
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
- **`decide()` and `Policy` are ported verbatim into `governance.py`** (ADR 0004) — keep them
  diffable against `vault_contrib.core`. What is deliberately *not* ported is the **value** of
  `flag_at`: Stage A's 0.85 is a title string ratio, here the score is cosine similarity on a
  specific model. `DEFAULT_POLICY` ships `flag_at = 1.0` — only an identical embedding flags.
  That is now the **measured** answer for `text-embedding-3-small`, not a placeholder: the
  corpus's closest legitimately-distinct pair scores 0.7406 and the weakest deliberate
  restatement scores 0.7500, a gap of 0.0094. Do not "restore" 0.85, and do not derive a
  threshold from the corpus distribution alone — it looks like a wide safe band above 0.74 and
  real duplicates live inside it. `flag_at` is derived per model by the two-sided procedure in
  `calibration.py` / `docs/embedding-calibration.md`; changing the constant needs a new row in
  that register. See ADR 0016 and its 2026-08-12 amendment.

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
- **The query request budget is three attempts at a 5s timeout**, settled by measurement on
  2026-08-12 (single-query p99 1.194s; a 128-document batch takes 0.728s). Worst case
  3 × 5s + 2 × 4s backoff = 23s, inside Heroku's 30s router budget. The realistic failure is a
  transient 429/502, not slowness. See "Deferred decisions" item 3 in
  `docs/vault-architecture.md`.
- **`VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt, and validated against the router budget
  at startup.** The retry constants live in `constants.py`, not in the adapter, precisely so
  `settings.py` can check them without importing a transport module — do not move them back.
  A unit test on the default constant is not sufficient and was not: it passed for months while
  every real environment carried 10, a 38s budget. A backfill wanting longer passes
  `timeout_seconds` to the provider directly; the adapter parameter is deliberately unbounded
  and the environment variable deliberately is not.
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

- **The credential dependency must not `yield` an open connection.** Sharing one checkout
  between authentication and the handler looks like free savings and is not: a dependency that
  yields holds the connection for the whole request, and `search`, `contribute` and `update`
  all call the embedding provider *between* their checkouts on purpose. It would pin a pooled
  connection across a 23s worst-case embedding budget, which is the same mistake as embedding
  inside a transaction, one layer up. Authentication takes its own short checkout and releases
  it. Considered and rejected 2026-08-14; see `docs/HANDOFF.md` task 15.
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

## The write path

- **Embed before the transaction, never inside it.** An embedding call is a third-party round
  trip; holding a transaction across it pins a pooled connection and the advisory lock for the
  provider's latency. Idempotency is therefore re-checked *under the lock*.
- **One corpus-wide `pg_advisory_xact_lock`** guards check-dedup-then-write. A per-key lock does
  not help: the conflict is between different idempotency keys.
- **No dedup, no write.** Missing embedding provider is 503, not a silent insert. The read path
  may degrade to lexical; the write path may not degrade to no-dedup.
- **Settled outcomes are 200**, including `flagged` and `rejected` — a client that retries a
  "flagged" as an error creates a second note that flags against the first.
- The idempotency digest covers the **validated model**, not raw bytes, so key order is not a
  409. `contributed_by` comes from the **credential**, never the body.
- `find_similar` applies `readable_path_predicate`: similarity output names and titles existing
  documents, so an unscoped dedup query is a disclosure channel around ADR 0014.
- `Merge` and `Link` raise. ADR 0004 keeps merge disabled and `link_at` unset; reaching either
  means a policy set a band nobody decided on.

## Rate limiting

- **Two layers, and they are not interchangeable.** The *quota* is a token bucket per
  (principal, operation) and enforces what an operator granted a credential. The *pre-auth
  guard* is IP-keyed and bounds the cost of authentication itself. Do not delete one as
  redundant with the other.
- **The quota is per authenticated principal, never per IP** — agents share egress addresses
  and a credential is what an operator can revoke. Limits mirror the integration spec's
  table; `LIMITS` is the single statement of them.
- **The pre-auth guard must stay a router-level dependency, never a route decorator.**
  FastAPI solves dependencies before calling the endpoint, and authentication *is* a
  dependency that queries `vault_agent_credentials` — so a `@limiter.limit` decorator on the
  endpoint charges after the database round trip it exists to prevent, protecting nothing.
  Attached to the `APIRouter` so new routes inherit it.
- **slowapi is used here, and that is not a boundary breach.** It is a third-party import;
  the vault builds its own `Limiter` and never touches the host's. `app/vault/` still contains
  no `from app.`. The guard's X-Forwarded-For key is forgeable and deliberately not an
  authorization boundary — forging it spreads load across buckets, it grants nothing.
- **Both layers are per process by default**, so the real ceiling is the limit times the
  worker count. Do not describe this as a hard limit in operator docs, and do not "fix" the
  quota in-process — the fix is a shared backend. The pre-auth guard can already take one via
  `VAULT_RATE_LIMIT_STORAGE_URI`, with slowapi's in-memory fallback so an unreachable Redis
  degrades the layer instead of failing requests.
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
