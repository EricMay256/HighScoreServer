# AGENTS.md — vault bounded context

Standing context for work under `app/vault/` and `vault_migrations/`. This file travels with
the package; the host repository's `AGENTS.md` governs everything outside these directories.

## What this is

The knowledge-platform bounded context: its own API models, domain records, Core tables,
repositories, services, auth, embeddings, and two transports — HTTP routes and an MCP
adapter. The package holds runtime code and schema definitions only — never corpus
content, credentials, exports, or vectors.

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
- Historical revision modules import only migration-owned helpers from
  `vault_migrations.helpers`, never the staged `app.vault` package. Alembic must be able to
  construct the full graph after the lineage directory is moved independently; runtime-only
  imports belong in `env.py` and are repointed during extraction.
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
- **Compile provenance is durable.** A wiki document's `compile_run_id` is required by the
  provenance consistency CHECK, so its foreign key uses `ON DELETE RESTRICT`. Never change it
  to `SET NULL`: PostgreSQL would attempt an update that the CHECK rejects, and provenance
  would be lost even if the CHECK were weakened.
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
- **`origin` carries upstream provenance; `contributed_by` and `created_at` stay the
  vault's.** When a corpus is replayed here, the credential that transmits a note is not the
  agent that wrote it and the moment the row lands is not when it was authored. `origin`
  JSONB holds the upstream `ContributedBy`, `CreatedAt`, `LastUpdated`, `Source` and
  `ClientRunID`; empty means this vault is the origin. Do **not** "fix" the import by letting
  the body set `contributed_by` — ADR 0016 takes it from the credential precisely so one
  principal cannot write under another's name — and do not backdate `created_at`, which would
  make the write ledger disagree with itself. Shape is a CHECK; the key set is closed in
  `origin.py` at the write boundary, per ADR 0009's precedent. Timestamps are stored as the
  ISO-8601 **text** they arrived as, so the export re-emits them verbatim. Contribution only:
  an update is a new body for an existing row, not a new provenance for it. See migration
  0010.
- **Adding an optional field to `VaultContributionRequest` does not need a
  `REQUEST_DIGEST_VERSION` bump.** `canonical_request_digest` dumps with `exclude_unset=True`
  since migration 0006, so a request that does not mention the new field serializes exactly as
  before. `tests/vault/test_origin.py` pins two pre-existing digests as the guard. Changing the
  digest *function* is still a bump; growing the model is not.
- **`vault_path`'s leaf name is the title's slug, and the folder is never caller-derived.**
  ADR 0022's 2026-08-20 amendment: a uuid filename makes the exported tree unbrowsable, and the
  exporter cannot rename it because ADR 0010 requires `vault_path` to equal the scanner's
  `rel_path`. `slug.slugify` collapses every non-alphanumeric run to a hyphen, so no separator
  survives a title into a path; the directory is a module constant. Collisions get `-2`, `-3`
  suffixes resolved **under the corpus advisory lock**, because `vault_path` is UNIQUE and the
  answer is only true while the lock is held — do not move that back to `_build_candidate`. A
  retitled note keeps its path: `replace_content` leaves it alone, and a path that followed the
  title would turn a frontmatter edit into a delete-plus-create in the export's git history.
- **A review candidate is always a brand-new note, and a settled case releases it.**
  `insert_pending` has one production caller: the contribute path's `Flag` branch, with the
  note it just wrote. Pre-existing notes appear only as JSON evidence, and the update path
  refuses on collision rather than opening a case. That is what licenses `rejected` to
  **delete** rather than archive — a candidate was never endorsed and its substance is
  already in the corpus. `candidate_document_id` is nullable and non-unique since migration
  0011: `delete` clears it so a judgement outlives what it judged, and a second case can be
  opened rather than overwriting the first. Only a **pending** case blocks retirement now;
  ADR 0019's "every state" rule made a flagged note permanently undeletable. `superseded` is
  reserved and unreachable — do not give it a meaning without a case that needs one.
- **The review surface is REST-only and stays off the MCP tool list.** Reading a case serves
  `flagged` content, the least-vetted text in the corpus, and deciding publishes or destroys
  a note. ADR 0021's defence is the privileged tool being absent from the surface injected
  text can name. A separate admin MCP is where these belong if they ever move.
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
  That is now the **measured** answer for `text-embedding-3-small`, not a placeholder: in the
  2026-08-15 with-tags counterfactual, the corpus's closest legitimately-distinct pair scores
  0.8318 and the weakest deliberate restatement scores 0.7500, so the bands overlap by 0.0818.
  Do not "restore" 0.85, and do not derive a threshold from the corpus distribution alone —
  real duplicates and legitimately adjacent notes do not separate cleanly. `flag_at` is derived
  per model by the two-sided procedure in `calibration.py` / `docs/embedding-calibration.md`;
  changing the constant needs a new row in that register. See ADR 0016 and its calibration
  amendments.

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
- Scopes are verbs, **one per route**. `vault:write` is *contribute only*; `vault:update` gates
  replacement and `vault:delete` gates retirement (ADR 0020). Do not re-fold them into one
  grant, and do not gate a new write route on an existing scope because adding one looks like
  ceremony — that is exactly how `vault:write` came to mean "may destroy any note". A quota is
  not an authorization boundary: `retire`'s tight bucket bounds how fast, not whether.
- The scope is `vault:delete` even though the route, service and quota bucket all say *retire*.
  The internal vocabulary describes the operation; the permission name warns the operator
  granting it, and "retire" reads as reversible when ADR 0019 makes it not.
- *What* a credential may read is ADR 0014's path policy, a property of the
  folder rather than of the credential.

## The MCP adapter

- **`routes.py` and `mcp.py` are two thin adapters over one service layer**, and neither may
  import the other. Anything both need — `canonical_request_digest`, `document_detail` —
  lives in `api_models.py`. A second copy in the second adapter is a silent drift bug: the
  digest decides idempotency, so two of them eventually disagree about what "the same
  request" is.
- **`list_tools` is filtered by the credential's scopes, and that is a security boundary.**
  The corpus is untrusted input written by agents and read by agents; a note carrying
  injected instructions is read *inside* an already-authenticated session, where no scope
  check intercepts it. What stops it is the destructive tool being absent from the surface
  the injected text can name. Do not advertise a tool the caller cannot use, and do not
  remove the per-tool scope check either — listing decides what an agent can see, the tool
  decides what it can do, and neither carries the boundary alone. See ADR 0021.
- **The mount inherits nothing from the host.** Not the router's pre-auth guard, not the
  exception handlers. `VaultMCPAuthMiddleware` carries the guard itself; removing it makes
  the MCP endpoint the only unbounded door on the vault, and nothing fails until someone
  hammers it.
- **The mounted app's lifespan does not run**, so `app.main` enters `vault_mcp_lifespan`
  explicitly. The session manager also refuses a second `run()`, which is why the app is
  built per `create_app()` and held on app state rather than cached at module level.
- **DNS-rebinding protection is off unless `VAULT_MCP_ALLOWED_HOSTS` is set.** The SDK
  default validates `Host` against `127.0.0.1` and would 421 every request to a public
  deployment. Do not "restore" the default.
- **The contribution tool derives its idempotency key from content.** A model asked for one
  invents a fresh value per attempt, which turns a retry into a duplicate note. It must hash
  the same canonical form `canonical_request_digest` does.
- The SDK's `token_verifier` is deliberately unused: it requires `AuthSettings.issuer_url`,
  which would publish OAuth discovery metadata for an authorization server that does not
  exist. That is the arm to replace if OAuth lands — `principal.resolve_credential` already
  has the `TokenVerifier` shape.

## The write path

- **Embed before the transaction, never inside it.** An embedding call is a third-party round
  trip; holding a transaction across it pins a pooled connection and the advisory lock for the
  provider's latency. Idempotency is therefore re-checked *under the lock*.
- **One corpus-wide `pg_advisory_xact_lock`** guards check-dedup-then-write. A per-key lock does
  not help: the conflict is between different idempotency keys. Retirement takes the same lock
  so its pending-review check cannot race with creation of a review case.
- **Update provider I/O is conditional; vector persistence is unconditional.** The candidate
  vector may be reused when its pre-lock digest matches, but every successful replacement
  upserts that candidate under the lock. Otherwise a concurrent writer can make the pre-lock
  decision stale and leave final text paired with the wrong embedding. See ADR 0018.
- **Pending review evidence is undeletable.** Retirement checks both the candidate foreign key
  and document IDs inside `similar_documents`; the latter is JSON and has no database FK. See
  ADR 0019.
- **No dedup, no write.** Missing embedding provider is 503, not a silent insert. The read path
  may degrade to lexical; the write path may not degrade to no-dedup.
- **Embedding context overflow is permanent input failure.** OpenAI documents an 8,192-token
  maximum for `text-embedding-3-small`; its context-length 400 is not retried and maps to 422
  on contribution and update. Other provider failures remain 503. Never infer permanence from
  every 400: an invalid model or dimensions setting is an operator error, not bad note content.
- **Settled outcomes are 200**, including `flagged` and `rejected` — a client that retries a
  "flagged" as an error creates a second note that flags against the first.
- The idempotency digest covers the **validated model**, not raw bytes, and v3 recursively sorts
  object keys, so top-level or nested key order is not a 409. List order remains significant.
  `contributed_by` comes from the **credential**, never the body. Any digest-rule change bumps
  `REQUEST_DIGEST_VERSION` and gets an Alembic revision; old rows retain their version and use
  the one-replay compatibility path in ADR 0016.
- Create and update inherit one content request model. Do not add normalization or collection
  validation to only one verb. Facet-name normalization rejects collisions rather than merging
  or silently overwriting caller data.
- An idempotent replay does not create a second document or write-request row, but it **does**
  append a `replayed` audit event carrying that inbound attempt's request ID. A retry must not
  disappear from incident reconstruction.
- Reusing a current-version idempotency key for a different body appends a `conflict` audit
  event before the service raises. The event uses a separate transaction because the
  transaction that detects and raises the conflict rolls back.
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
  no `from app.`. For the current direct-to-Heroku topology, key the guard from the rightmost
  `X-Forwarded-For` value: Heroku appends the address it observes after caller-controlled
  prefixes. If another proxy is placed in front of Heroku, revisit this assumption rather than
  guessing at a different list position.
- **A principal may be granted a wider quota, in code and never in configuration.**
  `PRINCIPAL_LIMITS` widens named operations for a named principal; `limit_for` is
  the single lookup and still raises on an operation `LIMITS` does not register, so
  an override cannot invent one. Today it grants `importer` bulk headroom on
  `contribute` and `update` only -- import writes, it does not search -- because the
  shared limits describe an interactive agent and at 30/min a 500-note corpus takes
  over four hours. Keying on the name is safe only because `docs/HANDOFF.md` already
  requires the importer to run as that principal; the write ledger is keyed
  `(principal_id, idempotency_key)`, so a different name bypasses the duplicate
  guard regardless. Do not move this to an environment variable: a quota a
  deployment can widen is a way to unlimit production by accident.
- **Unknown quota operations fail closed.** Every route operation must be registered in
  `LIMITS`; a typo or new operation without a deliberate quota is a programming error, not an
  unlimited bucket.
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
- `tests/vault/` is not one ownership unit. Follow the classification and standalone-fixture
  plan in `docs/vault-extraction-manifest.md`; never move the directory wholesale.
