# AGENTS.md — vault bounded context

Rules for work under `app/vault/` and `vault_migrations/`. This file travels with the
package; the host repository's `AGENTS.md` governs everything else. Read all of it — it is
short on purpose. **The why behind each rule is in
[`docs/invariants-rationale.md`](docs/invariants-rationale.md)** (cited as *rationale:
Section*) and in the ADRs under [`docs/adr/`](docs/adr/README.md). Read the rationale before
changing a rule that looks wrong; most of them look wrong in exactly the way that bit someone.

## What this is

The knowledge-platform bounded context: API models, domain records, Core tables,
repositories, services, auth, embeddings, and two transports (HTTP routes and an MCP adapter).
Runtime code and schema only — never corpus content, credentials, exports, or vectors. Hosted
here as a staging location; extraction is scheduled (ADR 0062, `docs/vault-extraction-manifest.md`).

**Purpose (2026-11-25):** improve the quality of the user's agents — persistent memory across
sessions and providers, judged by value per token of context. The agent loop lives in the
client (ADR 0053); retrieval tuning is frozen until 500 active documents (ADR 0055).

## Before you change a module, read

| Touching | Read first |
| --- | --- |
| `tables.py`, `repository.py`, `vault_migrations/` | ADRs 0001–0003; rationale: *Persistence*, *Schema invariants* |
| contribute / update / retire in `service.py` | 0016 (and its calibration amendments), 0018–0020, 0032; rationale: *The write path* |
| amendments, `body_diff.py` | 0028, 0033, 0036, 0044 |
| compile | 0027 |
| `export.py`, `wikilinks.py`, promotion | 0012, 0022, 0023, 0025, 0030, 0061 |
| `search.py`, `snippet.py`, embeddings | 0005–0007, 0013, 0031, 0034, 0055; `docs/embedding-calibration.md` |
| `mcp.py` | 0021, 0026, 0031, 0032; rationale: *The MCP adapter* |
| `oauth*.py`, `google_oidc.py`, `passwords.py` | 0024, 0029, 0037–0040; rationale: *The OAuth authorization server* |
| `auth.py`, `principal.py`, `rate_limit.py` | 0015, 0020; rationale: *Authentication*, *Rate limiting* |
| `read_policy.py`, `facets.py`, `origin.py` | 0010, 0014, 0017; migration 0010 |
| Human routes and services | 0048–0052, 0054 |

## Persistence and isolation

- SQLAlchemy 2.x **Core, never the ORM** (no `DeclarativeBase`, `Session`, `relationship`,
  `mapped_column`, `sessionmaker`). The host's raw-SQL ADR does not apply here. ADR 0001.
- API models, domain records, Core tables, repositories and services stay separate layers.
  Repositories take an `AsyncConnection` first and never acquire one; services own transactions.
- Core metadata is for query building and drift tests; it never reaches `create_all()` in
  production. Migrations are reviewed SQL in `vault_migrations/`, versioned in
  `vault.vault_alembic_version`, importing only `vault_migrations.helpers`.
- No imports between vault and leaderboard code in either direction; intra-package imports are
  **relative** — no `from app.` / `import app.`. `tests/vault/test_boundaries.py` enforces both.
- No cross-schema FKs, views, triggers, shared sequences or cross-domain transactions.
  Everything lives in the `vault` schema. `pgvector` is used only here.
- Docs and ADRs live in `app/vault/docs/`, never the host's `docs/`.

## Schema invariants

- Audit events carry correlation ids, **not FKs**; an audit insert must never fail on a
  constraint. `latency_ms` is nullable. ADR 0002.
- Embeddings live in `vault_document_embeddings` keyed `(document_id, profile_id)`; "not
  embedded" is the absence of a row; re-embedding is an upsert. One vector per document is
  enforced — chunking is a migration. ADRs 0003, 0034.
- A wiki row's `compile_run_id` FK is `ON DELETE RESTRICT`; never `SET NULL`.
- `kind` is lifecycle (`note`/`wiki`); `doc_type` is taxonomy, free text with a **shape-only**
  CHECK, vocabulary enforced from `types.yml` in code. Don't tighten the CHECK or widen `kind`. ADR 0009.
- `vault_path` is the **only** policy key — no `policy_scope` column. Port
  `inheritance.resolve_context`; don't reimplement prefix matching. ADR 0010.
- `status` is the visibility gate (closed enum, `routes.READABLE_STATUSES`); `doc_status` is the
  Status Map value. Never gate reads on `doc_status`. ADR 0011.
- `source_sha256` NULL means "authored in the database". Reconciliation is mark-and-sweep
  **scoped by path prefix**, after a complete walk, refusing an implausibly small one. ADR 0012.
- `facets` classify and are **never** embedded; `tags` describe and are. Facet names are a closed
  set in `facets.py`. A facet never gates a read. ADR 0017.
- `origin` holds upstream provenance; `contributed_by` comes from the credential and
  `created_at` is never backdated. Migration 0010.
- Adding an optional request field needs no `REQUEST_DIGEST_VERSION` bump
  (`exclude_unset=True`); changing the digest function does.
- `vault_path`'s leaf is the title's slug; the folder is never caller-derived; collision suffixes
  resolve **under the corpus advisory lock**; a retitled note keeps its path. ADR 0022.
- `promotion_status` and `vault_path` move together in one statement under the lock;
  `updated_at` does not move. A note is never a candidate at birth. ADRs 0023, 0061.
- `Agent/wiki/_index.md` is generated by the exporter, not stored; its timestamps come from the
  pages. The export **writes** more prefixes than it **prunes** (`EXPORTED_PATH_PREFIXES` ⊃
  `CORPUS_OWNED_PATH_PREFIXES`); the owned set is explicit, never derived from occupancy.
- Compilation: the service plans (`missing`/`stale`/`new-source`), a model writes. Plans carry
  ids, never bodies. A flagged note is never offered. Wiki pages are embedded but **excluded from
  dedup**. Declines (`compile_declined_at`) stop re-offers and expire when the note changes;
  `write_page`/`finish`/`fail` take the corpus lock; `source_ids` are re-validated under it. ADR 0027.
- A review candidate is always a brand-new note; `rejected` deletes it; only a **pending** case
  blocks retirement; `superseded` is reserved. ADR 0019.
- An amendment proposal is workflow state, never a document; applied only via `vault:review`;
  removed lines need explicit acknowledgement; a changed base settles `stale`. A span edit is
  converted to a `body_diff` inside `propose` and never stored as such; `occurrence=None` means
  "must be unique". Don't relax the diff grammar. ADRs 0028, 0033.
- `related_ids`/`source_ids` are shape-checked (a value that looks like a *name* is refused),
  never existence-checked, never a DDL CHECK. Inside the database every edge is an id;
  `wikilinks.py` translates at import/export. ADRs 0025, 0030.
- Embedding text is title + aliases + tags + summary + body, nothing else.
  `embedded_text_sha256` NULL means stale. ADR 0013.
- `search_vector` uses `vault.text_array_to_string` (IMMUTABLE); `VAULT_TEXT_SEARCH_CONFIG` is
  a migration-time choice, checked against the catalog at startup.
- `ai_read` is enforced twice and **fails closed**: excluded at import and filtered on both
  search arms, fusion hydration, and fetch. `get_by_id(readable_only=...)` defaults **off**. ADR 0014.
- `decide()` and `Policy` stay diffable against `vault_contrib.core`. `flag_at` is derived per
  model by the procedure in `docs/embedding-calibration.md`; never change it without a register
  row. ADR 0016.
- `collection` says whose write path owns a row; every mutation predicates on it, **defaulting
  to `agent`**. `resource_revision` moves on every domain-column write and stays ≥
  `content_revision`. Human history and feed tables have no FK to documents; feed writers hold
  `CORPUS_LOCK_KEY`. ADR 0049.

## The Human collection

- Two read audiences, **chosen by route**: ordinary routes apply `readable_path_predicate`;
  `/human/notes` applies `collection = 'human'` and no read policy. Never add an audience
  parameter to `/notes`. Agent *workflows* (`find_similar`, compile planning, `write_page`
  sources) exclude Human rows. ADR 0050.
- A Human write is one transaction under the corpus lock: row, then snapshot and feed entry via
  `VaultHumanHistoryRepository.record` (which checks `pg_locks` — keep that check), then audit.
  No embedding call, no dedup. Edit and move are retry-safe by state; create by the ledger. ADR 0051.
- Deletion leaves a tombstone; only `VaultHumanNoteService.restore` brings a note back, under
  its own id. The feed cursor names an entry and is verified before resuming (410 on mismatch);
  never reduce it to a position or order by `occurred_at`. ADR 0052.

## Retrieval and embeddings

- The embedding port names no vendor; only `embedding_runtime.py` maps a provider to an
  adapter. `settings.py` does not import transports.
- The provider is optional at runtime: no key means lexical-only search, reported as such.
  `vector_status` keeps `used` / `not_configured` / `failed` distinct.
- Query budget: three attempts at 5s; `VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt and
  validated against Heroku's 30s router budget at startup. Retry constants live in `constants.py`.
- Never log query text or an embedding exception's message — log the exception type.
- Search passes the text-search config as a bound parameter, always.
- Search returns **candidates, not documents** (ADR 0031). A field is added back only for
  *selection* value. Both adapters build responses via `api_models.search_response`.
  `snippet` is a lead extract, supplied only when `summary` is absent. `next_cursor` stays null.
- `tests/vault/test_mcp_budget.py` is a ratchet: lowering a budget is a deliverable, raising one
  needs a reason in the commit message. It counts both wire copies.
- Search returns `active` only; fetch also resolves `archived`, never `flagged`. ADR 0008.
- The lexical arm disjoins terms but keeps phrases and stays conjunctive under negation. ADR 0007.
- Profile `openai/text-embedding-3-small:1536`, fused by RRF. Changing model is a controlled
  re-embed, never a config change. ADRs 0005, 0006.
- **Frozen until 500 active readable documents:** RRF parameters, the disjunction rule, the
  snippet bound, a `ts_headline` arm, chunking, multi-query and batch fetch. ADR 0055.

## Authentication and authorization

- Agents authenticate with `hssv1_<credential-id>_<secret>`, split **from the right**; only
  `sha256(secret)` is stored; a miss still compares against a dummy hash. ADR 0015.
- The credential dependency must **not yield an open connection** across the request.
- `401` for bad/inactive, `403` for missing scope; neither says which check failed.
  `last_used_at` is written on success only.
- **Scopes are verbs, one per route** (`vault:write` = contribute; `update`, `delete`,
  `propose`, `review`, `compile` are separate). A route may share a verb only if it reaches
  nothing its holder could not already have written. ADRs 0020, 0035.
- Human and Agent scopes never share a credential (`vault:human-*` exclusive with every
  Agent-writing scope), enforced by CHECKs and at `authorize`. ADR 0049.
- OAuth (ADR 0024): an access token **is** a `vault_agent_credentials` row and an ordinary
  `hssv1_` string — no token table, no second format. The principal is `oauth-<client_id>`,
  never the client name. All OAuth state lives in Postgres, never process memory.
- Refresh tokens rotate and are marked `consumed_at`; replay or revocation burns the whole
  family. `load_authorization_code` never consumes. Nonce and code redemption is
  `DELETE ... RETURNING`; redeeming a nonce and minting its code is one transaction, with
  bcrypt run before it.
- **Never raise the MCP SDK's protocol errors inside a transaction block** — compute the
  outcome, leave the block, then raise. `test_boundaries.py` enforces this lexically.
- Entitlements belong to the refresh family (ADR 0029); codes carry baseline scopes only.
- `/register` writes no audit event and has its own bucket; consent shows the redirect origin
  and registration id and calls the client name unverified. Registrations are pruned by age
  **and liveness**. `authorize` and `delete_stale` share `OAUTH_CLIENT_LOCK_KEY`.
- A compile run belongs to the principal that opened it (409 otherwise); a compile `page_id` is
  kind-checked before any write.
- Operator identity: bcrypt password and/or Google OIDC, each independent; partial Google config
  is refused; one failure message for every failure. Absence of `VAULT_PUBLIC_URL` turns OAuth
  off. The login page uses the vault's own Jinja2 environment, autoescaped.
- A paged read that must see one corpus uses `REPEATABLE READ`.
- **The operator side is frozen** (2026-11-25): no new identity methods, console auth modes or
  entitlement shapes without an ADR.

## The MCP adapter

- `routes.py` and `mcp.py` are thin adapters over one service layer and never import each
  other; shared shapes live in `api_models.py`.
- Privileged tools live on the one mount, **gated by scope**; `list_tools` is filtered by the
  credential's scopes *and* each tool re-checks. That is the prompt-injection boundary. ADRs 0021, 0026.
- `vault_list_review_cases` returns no bodies. `similars` (notes-only, the verdict) and
  `related_pages` (wiki-only, context) come from separate queries.
- The mount inherits nothing from the host: `VaultMCPAuthMiddleware` carries the pre-auth
  guard; `app.main` enters `vault_mcp_lifespan` explicitly; the app is built per `create_app()`.
- DNS-rebinding protection is off unless `VAULT_MCP_ALLOWED_HOSTS` is set.
- The contribution tool derives its idempotency key from content, never from the model.
- A change to the tool surface is not finished until the `knowledge-vault` skill has been read
  against it (host AGENTS.md, "Shipping a change to clients").

## The write path

- **Embed before the transaction, never inside it**; re-check idempotency under the lock.
- One corpus-wide `pg_advisory_xact_lock` guards check-dedup-then-write and retirement.
- Update vector persistence is unconditional under the lock (ADR 0018). Pending review evidence
  is undeletable (ADR 0019).
- **No dedup, no write**: no provider is 503. Context overflow is a permanent 422; other
  provider failures 503.
- Settled outcomes are 200, including `flagged` and `rejected`.
- The digest covers the validated model with recursively sorted keys; `contributed_by` comes
  from the credential. A digest-rule change bumps `REQUEST_DIGEST_VERSION` with a migration.
- Create and update share one content model. A replay appends a `replayed` audit event; a
  conflicting key appends `conflict` in a separate transaction.
- `find_similar` applies `readable_path_predicate`. `Merge` and `Link` raise.

## Rate limiting

- Two layers, not interchangeable: a per-(principal, operation) token-bucket **quota**, and an
  IP-keyed **pre-auth guard** attached to the router, never a route decorator.
- Quotas are per principal, never per IP. `LIMITS` is the single statement; unknown operations
  fail closed. `PRINCIPAL_LIMITS` overrides live in code, never configuration (empty today).
- Key the guard from the rightmost `X-Forwarded-For` value (direct-to-Heroku topology).
- Both layers are per process; don't describe them as hard limits. Prune a bucket only when it
  would have refilled.

## Working agreements

- Type hints on every signature; no module-level side effects.
- Validate anything interpolated into SQL or DDL, even from an operator.
- Flag anything needing an Alembic revision; flag new dependencies and add them to the
  extraction manifest.
- Material decisions get a Nygard ADR in `docs/adr/`, claiming the number in its README first.
- `tests/vault/` is not one ownership unit; follow the manifest's classification.
- Keep this file short: a new rule gets one or two lines here and its reasoning in an ADR.
