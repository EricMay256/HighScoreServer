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
- **`promotion_status` routes through `vault_path`, and the two move together.** ADR 0023
  says candidacy is a field and the folder is a projection of it — but the projection is the
  path column, not a directory the exporter re-derives. ADR 0010 requires `vault_path` to stay
  byte-identical to the governance scanner's `rel_path`, so a file under
  `Agent/Promotion Candidates/` whose row still says `Agent/notes/` would resolve its
  `allowed_types` and `validation_mode` against the wrong `folders.yml` rule. `folders.yml`
  says the same thing from the other side: dropping a file into the folder by hand does
  nothing, because "the row still names its own path". `VaultPromotionService` therefore sets
  the field and resolves the new path in one statement, **under the corpus advisory lock** —
  `vault_path` is UNIQUE and collisions suffix, so the free name is only still free while the
  lock is held. `updated_at` deliberately does not move, and here that is load-bearing rather
  than tidy: the rendered file is byte-identical either side of the move, which is what makes
  git show a rename and follow the history. Home is keyed on `kind`, so a retracted **`Wiki
  Page` returns to `Agent/wiki/`**, never to `Agent/notes/`, which is typed to `Agent Note`
  alone. `NewVaultDocument` carries no `promotion_status` on purpose: a note is never a
  candidate at birth, and keeping the field off that record is what makes the review-gated
  verb the only way in. Active documents only — a candidate is "served to agents, returned by
  search, and inside the dedup gate", which describes `active` and nothing else.
- **`Agent/wiki/_index.md` is generated by the exporter, not stored as a row.** Every line in
  it derives from pages the corpus already holds, so a row would be the same facts twice. It
  joins the export's `expected` set explicitly — otherwise the prune sweep deletes the file it
  just wrote, since no row accounts for it. Both its timestamps come from the pages
  (earliest/latest), never from the clock: this module's contract is a zero-line diff on an
  unchanged corpus, and it parses no markdown, so Stage A's "stamp `now()` and read the old
  file for `CreatedAt`" is unavailable in both halves. It returns None when there are no wiki
  pages, which is what lets a stale index be pruned once `Agent/wiki/` is owned.
- **The export writes more prefixes than it prunes.** `EXPORTED_PATH_PREFIXES` is what may be
  written; `CORPUS_OWNED_PATH_PREFIXES` is the subset the service is authoritative for and may
  therefore delete from. `Agent/wiki/` is in the first and not the second, because the Stage-A
  librarian still holds 15 compiled pages there; it joins the second when compilation moves to
  the service. The owned set is explicit rather than derived from occupancy, per ADR 0023:
  "sweep the prefixes that have rows" looks equivalent and fails exactly when an owned folder
  empties — the last promotion candidate settles, no row names the prefix, the sweep skips it,
  and the stale file survives advertising a candidacy that ended.
- **Compilation plans; a model writes. And `find_similar` is notes-only.** ADR 0027. The
  service decides which pages are stale — three reasons, `missing` / `stale` / `new-source`,
  ported from the Stage-A engine and kept diffable against it — and a model writes the prose.
  A plan carries note **ids**, never bodies: the agent fetches through the policy-checked read
  surface rather than through a second one with its own disclosure rules. A **flagged note is
  never offered as a new source**, because compiling unendorsed content launders it into a tree
  the read surface serves freely.
  **Wiki pages are excluded from the dedup corpus**, and that is correctness rather than
  tuning: a page restates its sources by construction, so a note whose successor covers the
  same ground would look like a duplicate of a document that exists only because that note
  does. Pages are still *embedded* — search returning synthesis is the point — they are simply
  not adjudicated. Do not "restore" the filter for symmetry with the read path.
  A successful run publishes the frontier it was **planned** against — its own
  `input_frontier` — never one read at `finish`. Reading it at settlement loses notes
  permanently: a note landing after the plan is never offered to that run, and a finish-time
  maximum then covers its timestamp so no later plan offers it either. A **failed run
  publishes none** (a failed frontier would claim coverage for pages nobody wrote).
  **`write_page`, `finish` and `fail` all take the corpus advisory lock.** `write_page`'s
  check that the run is still running is only a guard because the settle path contends for the
  same lock; without it a page commits into a settled run. `source_ids` are re-validated under
  that lock too — the pre-embedding check goes stale across a third-party call that retirement
  can land inside. `compiled_at` comes from the run's start so one run is one
  timestamp, which is why `set_compile_provenance` exists apart from `replace_content` — that
  one leaves provenance alone so an ordinary update cannot claim to have compiled anything.
  `source_ids` are **validated** and refused when unresolved, unlike `related_ids`: provenance
  naming something that never existed is a false claim, not a dangling edge.
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
- **The review surface is REST *and* MCP, and the MCP half is scope-gated.** Reading a case
  serves `flagged` content, the least-vetted text in the corpus, and deciding publishes or
  destroys a note — so the tools exist only for a credential holding `vault:review`, which is
  ADR 0021's defence applied through `list_tools`. **ADR 0026 reversed the destination this
  invariant used to name:** there is no separate admin MCP, and the REST routes stay. See "The
  MCP adapter" below for the operating rule that carries the rest of it.
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

### The OAuth authorization server (ADR 0024)

- **`load_access_token` returns the OAuth *client id*, and None for an operator credential.**
  The SDK's revocation handler calls `revoke_token` only when the loaded token's `client_id`
  equals the authenticated client's, so returning `principal_id` (`oauth-<slug>`) made
  `/revoke` a silent 200 that revoked nothing. Returning None for a credential with no refresh
  family is deliberate: an `hssv1_` credential must not be revocable through an endpoint any
  self-registered client may call.
- **Revocation and replay both burn the whole family.** Revoking one credential leaves its
  refresh token able to mint a replacement, which is not revocation. And a `consume()` miss is
  not automatically innocent — the SDK loads and exchanges separately, so two concurrent
  requests both pass the `consumed_at` check and the loser is exactly the captured-token case
  rotation exists to catch. Re-read the row: consumed means burn the family.
- **The public OAuth routes carry the pre-auth guard as ASGI middleware.** They are
  root-mounted Starlette routes and inherit neither the vault router's dependency nor the MCP
  mount's; `/register` writes a row on every unauthenticated call. A route whose endpoint
  already has its own slowapi bucket is left unwrapped — wrapping the login POST suppressed
  its own tighter limit.
- **Registrations are pruned by age *and liveness*, never by `expires_at`.** The SDK leaves
  `client_secret_expiry_seconds` unset and nothing else supplies one, so an expiry-only sweep
  deleted nothing at all. A client with an unconsumed, unexpired refresh token is never a
  candidate — deleting one cascades to its tokens and revokes a working connector.
- **An issued access token *is* a `vault_agent_credentials` row.** That is the load-bearing
  choice and everything else follows: `contributed_by` derives from the principal, revocation
  and the credential census work unchanged, scopes stay on the credential, and quota buckets
  key on a principal an OAuth client has like any other. There is deliberately **no token
  table** — a parallel identity type would duplicate scopes, revocation, quotas and
  attribution, and every duplicate is somewhere the two paths could disagree.
- **All OAuth state lives in Postgres, never in process memory.** Registration arrives
  server-to-server from the vendor's backend while `/authorize` is a browser navigation, so the
  two halves reliably land on different Gunicorn workers. The spike used a dict and failed
  exactly there — deterministically, and only in production. Do not "optimise" any of the three
  stores into a cache.
- **`vault_oauth_clients.client_info` is JSONB, and must stay so.** RFC 7591 registrations carry
  metadata neither this schema nor the SDK's `OAuthClientInformationFull` anticipated;
  projecting into columns would drop whatever did not fit and need a migration each time the
  SDK grew a field. Persistence never imports the SDK model — the provider validates the blob
  at its own boundary, the same division `vault_documents.frontmatter` already draws.
- **Nonces and authorization codes are stored as SHA-256, and single use is
  `DELETE ... RETURNING`.** A check-then-mark is two statements a concurrent redemption can
  interleave, and redeeming a pending authorization is what mints a code — running it twice
  would issue two codes for one approval. Neither table has a `consumed_at`, on purpose.
  `load_authorization_code` must **not** consume: the SDK splits load from exchange, and a
  consuming load would destroy a code on a failed exchange the client is still entitled to
  retry.
- **`vault_oauth_authorization_codes.scopes` mirrors `vault_agent_credentials_scopes_known`,
  not the OAuth baseline.** `OAUTH_BASELINE_SCOPES` is what a client may *request*, enforced in
  application code; an operator may widen a specific credential afterwards, which ADR 0024 calls
  expected rather than exceptional. Tightening the column CHECK to the baseline would forbid the
  widened case at a layer no application code could permit.
- **`passwords.py` is bcrypt and duplicates `app/auth.py` on purpose.** `app/vault/` may contain
  no `from app.`, so a ten-line wrapper is cheaper than a host dependency the package cannot
  take with it. bcrypt rather than `auth.hash_secret`'s SHA-256 because ADR 0015's
  full-entropy reasoning does not transfer to a password a person chose — and because this runs
  once per authorization, not once per request. It is offloaded with `asyncio.to_thread`;
  bcrypt releases the GIL while hashing, so that genuinely moves the work rather than yielding.
- **The operator hash is configuration, not a row.** `VAULT_OPERATOR_PASSWORD_HASH`, because
  there is one of them, it has no lifecycle a table would model, and a database's backups
  circulate more widely than a config var. Unset means the password method is not configured
  and the login **refuses** — never "any password works".
- **One failure message, whatever failed.** A wrong password, an expired nonce, a nonce that
  never existed, a bad CSRF token and an unconfigured operator password all render identically,
  which is why `redeem` returns None for every case rather than distinguishing them. A page that
  told them apart would hand an attacker a probe for valid authorization attempts.
- **The access token an OAuth client receives is an ordinary `hssv1_` string.** That is what
  lets the whole resource server — the MCP mount, the REST routes, scope checks, quotas,
  `contributed_by` — stay untouched by OAuth. Do not invent a second token format; if one ever
  seems necessary, the thing to change is this property, deliberately, in an ADR.
- **Refresh tokens rotate, and `vault_oauth_refresh_tokens` marks `consumed_at` rather than
  deleting.** The other two transient OAuth tables delete on redemption; this one must not, and
  the difference is a security property rather than a preference. A deleted row cannot be
  distinguished from a token that never existed, while a consumed one is positive evidence that
  a token was captured — so presenting one revokes every credential in its `family_id` chain
  and burns every unconsumed token in it. OAuth 2.1 requires rotation *with replay detection*
  for a public client, and this is the detection half. `load_refresh_token` is where it fires,
  which is untidy and unavoidable: the SDK offers no hook between recognising a refresh token
  and refusing it.
- **Rotation mints a new credential and revokes the old one.** It cannot re-key: only
  `sha256(secret)` is stored, so there is no way to hand back a token for an existing row.
  Revoked credential rows therefore accumulate, one per refresh, and want pruning alongside
  `vault_oauth_clients`. They grant nothing in the meantime.
- **`load_authorization_code` must not consume.** The SDK splits load from exchange and does
  real work between them — PKCE, the `redirect_uri` round trip, expiry — so a consuming load
  would destroy a code whenever any of those failed, when the client may still retry.
- **The login POST redeems the nonce before verifying the password**, so one authorization
  affords exactly one attempt. Do not "fix" that ordering to be friendlier: it is what stops a
  live authorization being reused as a guessing oracle.
- **Absence of `VAULT_PUBLIC_URL` is the feature's off switch.** Every URL in the discovery
  metadata is absolute, so a deployment that cannot state its own origin cannot serve correct
  metadata — the variable that makes it work is the variable that enables it, and forgetting it
  fails closed. Do not add a separate boolean; that would be a way to set one and not the other.
- **The login page uses the vault's own Jinja2 environment (`templating.py`), never HSS's
  `templates/`.** The boundary test scans *imports*, so a `{% extends "base.html" %}` would pass
  every guard in this repository and fail only at extraction, as a missing file. Autoescaping is
  on and must stay on: registration is open, so `client_name` is attacker-controlled text
  rendered next to a password field.

## The MCP adapter

- **`routes.py` and `mcp.py` are two thin adapters over one service layer**, and neither may
  import the other. Anything both need — `canonical_request_digest`, `document_detail` —
  lives in `api_models.py`. A second copy in the second adapter is a silent drift bug: the
  digest decides idempotency, so two of them eventually disagree about what "the same
  request" is.
- **Privileged tools live on the one mount, gated by scope (ADR 0026).** There is no separate
  admin MCP; the proposal for one was considered and rejected. `vault_list_review_cases`,
  `vault_read_review_case`, `vault_decide_review_case` and `vault_set_promotion_status` all
  require `vault:review`, so a session without it neither sees nor can name them — the same
  boundary that hides `vault_retire_note`.
  **The operating rule that carries the rest: a reviewing credential holds `vault:read` and
  `vault:review`, and nothing else.** Then adjudication cannot also retire or overwrite. That
  rule is configuration rather than code, which is the deliberate cost — a separate mount
  would have made the consumer surface structurally incapable of destruction, and this does
  not. Re-open ADR 0026 if a second person gains a credential, or if an agent starts
  adjudicating unattended.
  `vault_list_review_cases` returns **no note bodies**: triage must not pull the least-vetted
  text in the corpus into context. Only reading a specific case does that.
- **`similars` is the verdict; `related_pages` is context, and they come from different
  corpora.** `find_similar` is notes-only and feeds `decide()` and `top_similarity`;
  `find_related_pages` is wiki-only and reaches the response and nothing else. Two queries
  rather than one split afterwards, because a shared limit would let page hits starve the
  gate's evidence. Never let a page reach the gate or the calibration register — that is
  ADR 0027's whole point — and never withhold one from the response merely because the same
  query used to serve both purposes.
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
