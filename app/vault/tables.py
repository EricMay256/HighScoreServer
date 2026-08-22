"""Schema-qualified SQLAlchemy Core metadata for vault persistence.

Alembic is the only production migration mechanism. This metadata exists for
query construction and schema-drift tests; application code must never call
``metadata.create_all()``.
"""

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TSVECTOR, UUID

from .constants import EMBEDDING_DIMENSIONS, resolve_text_search_config


VAULT_SCHEMA = "vault"

# Resolved once at import so this metadata describes the same generated column
# the migration emitted. The startup assertion compares it against the
# expression actually stored in the catalog.
TEXT_SEARCH_CONFIG = resolve_text_search_config()

metadata = MetaData(schema=VAULT_SCHEMA)

document_kind_enum = ENUM(
    "note",
    "wiki",
    name="vault_document_kind",
    schema=VAULT_SCHEMA,
)
document_status_enum = ENUM(
    "active",
    "flagged",
    "archived",
    name="vault_document_status",
    schema=VAULT_SCHEMA,
)
review_state_enum = ENUM(
    "pending",
    "accepted",
    "rejected",
    "superseded",
    name="vault_review_state",
    schema=VAULT_SCHEMA,
)
promotion_status_enum = ENUM(
    "candidate",
    "promoted",
    "retracted",
    name="vault_promotion_status",
    schema=VAULT_SCHEMA,
)
write_request_state_enum = ENUM(
    "processing",
    "inserted",
    "flagged",
    "invalid",
    "failed",
    name="vault_write_request_state",
    schema=VAULT_SCHEMA,
)
compile_run_state_enum = ENUM(
    "running",
    "succeeded",
    "failed",
    name="vault_compile_run_state",
    schema=VAULT_SCHEMA,
)

vault_compile_runs = Table(
    "vault_compile_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("compiler_principal_id", Text, nullable=False),
    Column(
        "state",
        compile_run_state_enum,
        nullable=False,
        server_default=text("'running'"),
    ),
    Column(
        "started_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("completed_at", DateTime(timezone=True)),
    Column(
        "input_frontier",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column(
        "output_frontier",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("error_summary", Text),
    CheckConstraint(
        "btrim(compiler_principal_id) <> ''",
        name="vault_compile_runs_principal_nonempty",
    ),
    CheckConstraint(
        "(state = 'running' AND completed_at IS NULL) "
        "OR (state <> 'running' AND completed_at IS NOT NULL)",
        name="vault_compile_runs_completion_consistent",
    ),
)

vault_documents = Table(
    "vault_documents",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "kind",
        document_kind_enum,
        nullable=False,
        server_default=text("'note'"),
    ),
    # The governance Type Dictionary value. Deliberately TEXT rather than a
    # second enum: types.yml is meant to evolve without a migration, and an
    # enum would put the slower mechanism in charge of the faster concept.
    # Nullable because untyped is a real state. See ADR 0009.
    Column("doc_type", Text),
    # Vault-root-relative posix path, extension included -- the same rel_path
    # the governance engine matches folder rules against, and the key tying a
    # row to its file. See ADR 0010.
    Column("vault_path", Text, nullable=False),
    Column(
        "status",
        document_status_enum,
        nullable=False,
        server_default=text("'active'"),
    ),
    # The Status Map value from types.yml. Distinct from `status`, which is the
    # read surface's visibility gate and cannot represent a Wiki Page's
    # Current/Stub. See ADR 0011.
    Column("doc_status", Text),
    # Whether a human has proposed this note for the Human layer. A third
    # independent question alongside `status` and `doc_status`, and NULL --
    # never proposed -- is the ordinary state. `candidate` is the only value
    # the export routes on; `promoted` and `retracted` record that the
    # judgement was made and settled. See ADR 0023.
    Column("promotion_status", promotion_status_enum),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("body", Text, nullable=False),
    Column(
        "tags",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    # Alternative titles. Weighted 'A' in search_vector alongside the title,
    # because an alias is exactly the term a searcher types. See ADR 0013.
    Column(
        "aliases",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    # Frontmatter keys the schema does not model. The projector has to re-emit
    # notes the validator accepts, and global.yml's known_extra_keys makes a
    # column-per-key impossible. Distinct from `provenance`, which records how
    # the row got here rather than what the note said.
    Column(
        "frontmatter",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    # Classification that relates notes to each other -- project, area, system
    # -- as {name: [values]}. A column rather than namespaced entries in `tags`
    # because ADR 0013 embeds `tags`, and a shared tag inflates pairwise cosine
    # by ~0.04 against a dedup margin of 0.0094. Never read by
    # assemble_embedding_text, which is the whole point. See ADR 0017.
    Column(
        "facets",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    # SHA-256 of the upstream file. NULL means there is no upstream file: the
    # row was authored here, so nothing on disk governs it. See ADR 0012.
    Column("source_sha256", LargeBinary),
    Column(
        "related_ids",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column(
        "source_ids",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column("contributed_by", Text, nullable=False),
    Column("source_url", Text),
    Column(
        "provenance",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    # Where the content came from before it was contributed here: the upstream
    # author, timestamps, source reference and run id. Distinct from
    # `provenance`, which is the service's record of how the row got here and is
    # never caller-supplied. Empty means this vault is the origin. See ADR 0016
    # on why `contributed_by` cannot carry the author, and migration 0010.
    Column(
        "origin",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("schema_version", Integer, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}'::regconfig, "
            "coalesce(title, '')), 'A') || "
            f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}'::regconfig, "
            "coalesce(vault.text_array_to_string(aliases, ' '), '')), 'A') || "
            f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}'::regconfig, "
            "coalesce(summary, '')), 'B') || "
            f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}'::regconfig, "
            "coalesce(body, '')), 'C')",
            persisted=True,
        ),
        nullable=False,
    ),
    Column(
        "compile_run_id",
        UUID(as_uuid=True),
        ForeignKey(
            "vault.vault_compile_runs.id",
            name="vault_documents_compile_run_id_fkey",
            ondelete="RESTRICT",
        ),
    ),
    Column("compiled_by", Text),
    Column("compiled_at", DateTime(timezone=True)),
    CheckConstraint("btrim(id) <> ''", name="vault_documents_id_nonempty"),
    CheckConstraint("btrim(title) <> ''", name="vault_documents_title_nonempty"),
    CheckConstraint("btrim(body) <> ''", name="vault_documents_body_nonempty"),
    CheckConstraint(
        "btrim(contributed_by) <> ''",
        name="vault_documents_contributor_nonempty",
    ),
    CheckConstraint(
        "schema_version > 0",
        name="vault_documents_schema_version_positive",
    ),
    # Shape only, never vocabulary: which names are legal belongs to types.yml
    # and is checked in application code, so that adding a type stays a data
    # change rather than a migration.
    CheckConstraint(
        "doc_type IS NULL OR doc_type ~ '^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$'",
        name="vault_documents_doc_type_format",
    ),
    CheckConstraint(
        "doc_status IS NULL "
        "OR doc_status ~ '^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$'",
        name="vault_documents_doc_status_format",
    ),
    # Shape only. See the migration for why '[.]' and chr(92) are spelled this
    # way rather than with backslash escapes.
    CheckConstraint(
        "btrim(vault_path) <> '' "
        "AND vault_path !~ '^/' "
        "AND vault_path !~ '/$' "
        "AND vault_path !~ '//' "
        "AND vault_path !~ '(^|/)[.][.]?(/|$)' "
        "AND strpos(vault_path, chr(92)) = 0 "
        "AND length(vault_path) <= 1024",
        name="vault_documents_vault_path_format",
    ),
    CheckConstraint(
        "source_sha256 IS NULL OR octet_length(source_sha256) = 32",
        name="vault_documents_source_sha256_length",
    ),
    # An object of arrays of non-blank strings. The predicate lives in a
    # function because PostgreSQL rejects a subquery inside a CHECK, and
    # walking a JSONB object needs jsonb_each. See migration 0005 and ADR 0017.
    CheckConstraint(
        "vault.jsonb_is_facet_map(facets)",
        name="vault_documents_facets_shape",
    ),
    # Shape only, same construction and the same reason. See migration 0010.
    CheckConstraint(
        "vault.jsonb_is_origin_map(origin)",
        name="vault_documents_origin_shape",
    ),
    UniqueConstraint("vault_path", name="vault_documents_vault_path_key"),
    CheckConstraint(
        "(kind = 'note' AND compile_run_id IS NULL "
        "AND compiled_by IS NULL AND compiled_at IS NULL) "
        "OR (kind = 'wiki' AND compile_run_id IS NOT NULL "
        "AND compiled_by IS NOT NULL AND compiled_at IS NOT NULL)",
        name="vault_documents_compile_provenance_consistent",
    ),
)

# Embeddings live beside the documents rather than on them so a corpus can hold
# two profiles at once during a re-embed, and so "not yet embedded" is the
# absence of a row rather than a nullable column.
vault_document_embeddings = Table(
    "vault_document_embeddings",
    metadata,
    Column(
        "document_id",
        Text,
        ForeignKey(
            "vault.vault_documents.id",
            name="vault_document_embeddings_document_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("profile_id", Text, nullable=False),
    Column("embedding", VECTOR(EMBEDDING_DIMENSIONS), nullable=False),
    Column(
        "embedded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    # SHA-256 of the text this vector was built from. NULL means unknown, which
    # a re-embed job must treat as stale. Lives here rather than on the
    # document because staleness is per profile. See ADR 0013.
    Column("embedded_text_sha256", LargeBinary),
    PrimaryKeyConstraint(
        "document_id",
        "profile_id",
        name="vault_document_embeddings_pkey",
    ),
    CheckConstraint(
        "embedded_text_sha256 IS NULL "
        "OR octet_length(embedded_text_sha256) = 32",
        name="vault_document_embeddings_text_sha256_length",
    ),
    CheckConstraint(
        "profile_id ~ '^[A-Za-z0-9._:/-]{3,128}$'",
        name="vault_document_embeddings_profile_id_format",
    ),
)

vault_review_cases = Table(
    "vault_review_cases",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    # Nullable, and not unique. The pointer is cleared when a candidate is
    # retired, so the judgement outlives what it judged -- the same shape
    # `vault_write_requests.document_id` uses for the same reason. Not unique
    # because a decision that turns out wrong needs a second case rather than
    # an overwrite of the first. See migration 0011 and ADR 0019's amendment.
    Column(
        "candidate_document_id",
        Text,
        ForeignKey(
            "vault.vault_documents.id",
            name="vault_review_cases_candidate_document_id_fkey",
        ),
    ),
    Column(
        "state",
        review_state_enum,
        nullable=False,
        server_default=text("'pending'"),
    ),
    Column("reason", Text, nullable=False),
    Column(
        "similar_documents",
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("decided_at", DateTime(timezone=True)),
    Column("decided_by", Text),
    Column("decision_note", Text),
    CheckConstraint(
        "btrim(reason) <> ''",
        name="vault_review_cases_reason_nonempty",
    ),
    CheckConstraint(
        "(state = 'pending' AND decided_at IS NULL AND decided_by IS NULL) "
        "OR (state <> 'pending' AND decided_at IS NOT NULL "
        "AND decided_by IS NOT NULL)",
        name="vault_review_cases_decision_consistent",
    ),
)

vault_write_requests = Table(
    "vault_write_requests",
    metadata,
    Column("principal_id", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_sha256", LargeBinary, nullable=False),
    # Which rule produced request_sha256. Without it the digest comparison
    # depends on the server's schema version rather than the caller's payload --
    # see migration 0006 and ADR 0016's amendment.
    Column(
        "digest_version",
        SmallInteger,
        nullable=False,
        server_default=text("3"),
    ),
    Column(
        "state",
        write_request_state_enum,
        nullable=False,
        server_default=text("'processing'"),
    ),
    Column(
        "document_id",
        Text,
        ForeignKey(
            "vault.vault_documents.id",
            name="vault_write_requests_document_id_fkey",
        ),
    ),
    Column("response", JSONB),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("completed_at", DateTime(timezone=True)),
    PrimaryKeyConstraint(
        "principal_id",
        "idempotency_key",
        name="vault_write_requests_pkey",
    ),
    CheckConstraint(
        "btrim(principal_id) <> ''",
        name="vault_write_requests_principal_nonempty",
    ),
    CheckConstraint(
        "idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'",
        name="vault_write_requests_idempotency_key_format",
    ),
    CheckConstraint(
        "octet_length(request_sha256) = 32",
        name="vault_write_requests_sha256_length",
    ),
    CheckConstraint(
        "digest_version > 0",
        name="vault_write_requests_digest_version_positive",
    ),
    CheckConstraint(
        "(state = 'processing' AND completed_at IS NULL) "
        "OR (state <> 'processing' AND completed_at IS NOT NULL)",
        name="vault_write_requests_completion_consistent",
    ),
)

vault_agent_credentials = Table(
    "vault_agent_credentials",
    metadata,
    Column("id", Text, primary_key=True),
    Column("principal_id", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("secret_sha256", LargeBinary, nullable=False),
    Column(
        "scopes",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("last_used_at", DateTime(timezone=True)),
    CheckConstraint(
        "id ~ '^[A-Za-z0-9_-]{8,64}$'",
        name="vault_agent_credentials_id_format",
    ),
    CheckConstraint(
        "btrim(principal_id) <> ''",
        name="vault_agent_credentials_principal_nonempty",
    ),
    CheckConstraint(
        "btrim(display_name) <> ''",
        name="vault_agent_credentials_display_name_nonempty",
    ),
    CheckConstraint(
        "octet_length(secret_sha256) = 32",
        name="vault_agent_credentials_sha256_length",
    ),
    # Mirrors migration 0007. 'vault:write' is contribute only; replacement and
    # deletion are their own verbs, so a credential that may add a note does not
    # thereby may destroy one.
    CheckConstraint(
        "scopes <@ ARRAY['vault:read', 'vault:write', 'vault:update', "
        "'vault:delete', 'vault:review', 'vault:compile', "
        "'vault:export']::text[]",
        name="vault_agent_credentials_scopes_known",
    ),
)

# The OAuth authorization server's state, per ADR 0024. Deliberately *not* a
# token store: an issued access token is a `vault_agent_credentials` row, so
# scopes, revocation, quotas and attribution keep working through one mechanism
# instead of two that could disagree.
#
# All three live in Postgres rather than process memory because registration
# arrives server-to-server from the vendor's backend while `/authorize` is a
# browser navigation, and the two halves land on different workers. See
# migration 0013.
vault_oauth_clients = Table(
    "vault_oauth_clients",
    metadata,
    Column("client_id", Text, primary_key=True),
    # The SDK's OAuthClientInformationFull, dumped faithfully. JSONB rather
    # than a column per field: RFC 7591 registrations carry metadata this
    # schema did not anticipate, and projecting into columns would drop
    # whatever did not fit. Same reasoning as vault_documents.frontmatter.
    Column("client_info", JSONB, nullable=False),
    Column(
        "registered_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    # Open registration means unbounded rows, so they expire and get pruned.
    # NULL is a client that does not, which an operator may create deliberately.
    Column("expires_at", DateTime(timezone=True)),
    CheckConstraint(
        "btrim(client_id) <> ''",
        name="vault_oauth_clients_client_id_nonempty",
    ),
    CheckConstraint(
        "jsonb_typeof(client_info) = 'object'",
        name="vault_oauth_clients_info_is_object",
    ),
)

# One in-flight authorization, waiting for the operator to come back from the
# login form. Keyed by the SHA-256 of a nonce the vault mints, never the nonce
# itself (ADR 0015's rule for machine-generated secrets). Redeemed by
# DELETE ... RETURNING, which is why there is no `consumed_at`: a
# check-then-mark is two statements a concurrent redemption can interleave.
vault_oauth_pending_authorizations = Table(
    "vault_oauth_pending_authorizations",
    metadata,
    Column("nonce_sha256", LargeBinary, primary_key=True),
    Column(
        "client_id",
        Text,
        ForeignKey(
            "vault.vault_oauth_clients.client_id",
            name="vault_oauth_pending_authorizations_client_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    # The SDK's AuthorizationParams, including the PKCE code_challenge, which
    # waits here until /token redeems the code this becomes.
    Column("params", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    # SHA-256 of the CSRF token the login form carries in a hidden field.
    # Server-side rather than a signed token, because signing would need a third
    # secret to configure and rotate, while a row already exists per
    # authorization and gives single use for free. See migration 0014.
    Column("csrf_sha256", LargeBinary),
    CheckConstraint(
        "octet_length(nonce_sha256) = 32",
        name="vault_oauth_pending_nonce_length",
    ),
    CheckConstraint(
        "csrf_sha256 IS NULL OR octet_length(csrf_sha256) = 32",
        name="vault_oauth_pending_csrf_length",
    ),
    CheckConstraint(
        "jsonb_typeof(params) = 'object'",
        name="vault_oauth_pending_params_is_object",
    ),
    CheckConstraint(
        "expires_at > created_at",
        name="vault_oauth_pending_expires_after_creation",
    ),
)

# An authorization code between the login form and /token. Same hashing and
# same single-use idiom as the pending authorization above.
vault_oauth_authorization_codes = Table(
    "vault_oauth_authorization_codes",
    metadata,
    Column("code_sha256", LargeBinary, primary_key=True),
    Column(
        "client_id",
        Text,
        ForeignKey(
            "vault.vault_oauth_clients.client_id",
            name="vault_oauth_authorization_codes_client_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    # A column rather than a key inside a blob, unlike client_info: this set is
    # the vault's own and is copied straight onto vault_agent_credentials.scopes
    # when the code is redeemed, so the two carry the same shape.
    Column(
        "scopes",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column("code_challenge", Text, nullable=False),
    Column("redirect_uri", Text, nullable=False),
    Column("redirect_uri_provided_explicitly", Boolean, nullable=False),
    Column("resource", Text),
    Column("subject", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "octet_length(code_sha256) = 32",
        name="vault_oauth_codes_code_length",
    ),
    CheckConstraint(
        "btrim(code_challenge) <> ''",
        name="vault_oauth_codes_challenge_nonempty",
    ),
    CheckConstraint(
        "btrim(redirect_uri) <> ''",
        name="vault_oauth_codes_redirect_uri_nonempty",
    ),
    CheckConstraint(
        "expires_at > created_at",
        name="vault_oauth_codes_expires_after_creation",
    ),
    # Mirrors vault_agent_credentials_scopes_known rather than the narrower
    # OAuth baseline. ADR 0024 makes the baseline what a client may *request*,
    # enforced in application code, while an operator may widen a specific
    # credential afterwards -- a stricter constraint here would forbid a code
    # minted for a widened client, which the ADR calls expected rather than
    # exceptional.
    CheckConstraint(
        "scopes <@ ARRAY['vault:read', 'vault:write', 'vault:update', "
        "'vault:delete', 'vault:review', 'vault:compile', "
        "'vault:export']::text[]",
        name="vault_oauth_codes_scopes_known",
    ),
)

# Refresh tokens, rotated on every use with replay detection. OAuth 2.1 requires
# a public client's refresh token to be sender-constrained or rotated with
# detection, and rotation is the one available here.
#
# `consumed_at` rather than the DELETE ... RETURNING the other transient tables
# use, and the difference is the point: presenting an already-rotated refresh
# token is positive evidence a token was captured, and the response is revoking
# the whole `family_id` chain rather than failing one request. That needs the
# consumed digest remembered. See migration 0014.
vault_oauth_refresh_tokens = Table(
    "vault_oauth_refresh_tokens",
    metadata,
    Column("token_sha256", LargeBinary, primary_key=True),
    # Constant across every rotation descending from one authorization, which
    # is what makes "revoke the chain" expressible. Not a foreign key: the
    # family outlives any particular row in it.
    Column("family_id", UUID(as_uuid=True), nullable=False),
    Column(
        "client_id",
        Text,
        ForeignKey(
            "vault.vault_oauth_clients.client_id",
            name="vault_oauth_refresh_tokens_client_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    # The access credential this token renews. A rotation revokes this one as it
    # mints the next. CASCADE fires only on a real delete -- ordinary revocation
    # sets `revoked_at` and leaves the row in place.
    Column(
        "credential_id",
        Text,
        ForeignKey(
            "vault.vault_agent_credentials.id",
            name="vault_oauth_refresh_tokens_credential_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column(
        "scopes",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column("subject", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    CheckConstraint(
        "octet_length(token_sha256) = 32",
        name="vault_oauth_refresh_token_length",
    ),
    CheckConstraint(
        "expires_at > created_at",
        name="vault_oauth_refresh_expires_after_creation",
    ),
    CheckConstraint(
        "scopes <@ ARRAY['vault:read', 'vault:write', 'vault:update', "
        "'vault:delete', 'vault:review', 'vault:compile', "
        "'vault:export']::text[]",
        name="vault_oauth_refresh_scopes_known",
    ),
)

vault_audit_events = Table(
    "vault_audit_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("principal_id", Text),
    Column("operation", Text, nullable=False),
    Column("target_type", Text),
    Column("target_id", Text),
    Column("idempotency_key", Text),
    Column("outcome", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("trace_id", Text),
    # Nullable: lifecycle and system-generated events have no meaningful
    # latency, and NOT NULL would force a fabricated zero.
    Column("latency_ms", Double),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    # principal_id and idempotency_key are correlation identifiers, deliberately
    # not a foreign key: an audit insert must never fail on a referential
    # constraint, events for rejected or unauthenticated writes must keep their
    # key, and vault_write_requests must stay prunable. See vault ADR 0002.
    CheckConstraint(
        "btrim(operation) <> ''",
        name="vault_audit_events_operation_nonempty",
    ),
    CheckConstraint(
        "(target_type IS NULL AND target_id IS NULL) OR "
        "(target_type IS NOT NULL AND btrim(target_type) <> '' "
        "AND target_id IS NOT NULL AND btrim(target_id) <> '')",
        name="vault_audit_events_target_consistent",
    ),
    CheckConstraint(
        "idempotency_key IS NULL "
        "OR idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'",
        name="vault_audit_events_idempotency_key_format",
    ),
    CheckConstraint(
        "btrim(outcome) <> ''",
        name="vault_audit_events_outcome_nonempty",
    ),
    CheckConstraint(
        "btrim(request_id) <> ''",
        name="vault_audit_events_request_id_nonempty",
    ),
    CheckConstraint(
        "latency_ms >= 0",
        name="vault_audit_events_latency_nonnegative",
    ),
)

Index(
    "idx_vault_documents_search_vector",
    vault_documents.c.search_vector,
    postgresql_using="gin",
)
Index("idx_vault_documents_tags", vault_documents.c.tags, postgresql_using="gin")

# jsonb_path_ops, not the default jsonb_ops: a facet filter is a containment
# test (@>) and nothing else, and that operator class indexes only containment
# for a fraction of the size. It does not support the existence operators
# (?, ?|, ?&) -- a query needing those wants its own index, not a widened one.
Index(
    "idx_vault_documents_facets",
    vault_documents.c.facets,
    postgresql_using="gin",
    postgresql_ops={"facets": "jsonb_path_ops"},
)
# Every folders.yml glob is a literal prefix plus '/**', so resolving a
# document's policy context is a longest-prefix match. The UNIQUE index uses
# the default collation and cannot serve LIKE 'prefix%' unless the database is
# C-collated; text_pattern_ops always can.
Index(
    "idx_vault_documents_vault_path_prefix",
    vault_documents.c.vault_path,
    postgresql_ops={"vault_path": "text_pattern_ops"},
)
Index(
    "idx_vault_documents_kind_status_updated",
    vault_documents.c.kind,
    vault_documents.c.status,
    vault_documents.c.updated_at.desc(),
)
# No partial predicate: embedding is NOT NULL, so every row is indexable. With a
# single unpartitioned index, profile filtering is a post-filter; once a second
# profile is populated the remedy is a partial HNSW index per profile, added by
# a migration at that time. See vault ADR 0003.
Index(
    "idx_vault_document_embeddings_hnsw",
    vault_document_embeddings.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
Index(
    "idx_vault_document_embeddings_profile",
    vault_document_embeddings.c.profile_id,
)
Index(
    "idx_vault_review_cases_state_created",
    vault_review_cases.c.state,
    vault_review_cases.c.created_at,
)
Index(
    "idx_vault_write_requests_created",
    vault_write_requests.c.created_at,
)
Index(
    "idx_vault_agent_credentials_principal",
    vault_agent_credentials.c.principal_id,
)
Index(
    "idx_vault_audit_events_principal_occurred",
    vault_audit_events.c.principal_id,
    vault_audit_events.c.occurred_at.desc(),
)
Index("idx_vault_audit_events_request", vault_audit_events.c.request_id)
Index(
    "idx_vault_audit_events_trace",
    vault_audit_events.c.trace_id,
    postgresql_where=vault_audit_events.c.trace_id.is_not(None),
)
# Replaces the index the dropped composite foreign key used to provide, keeping
# "which audit events belong to this write request" a cheap lookup.
Index(
    "idx_vault_audit_events_principal_idempotency",
    vault_audit_events.c.principal_id,
    vault_audit_events.c.idempotency_key,
    postgresql_where=vault_audit_events.c.idempotency_key.is_not(None),
)
Index(
    "idx_vault_compile_runs_state_started",
    vault_compile_runs.c.state,
    vault_compile_runs.c.started_at.desc(),
)

# Pruning walks all three OAuth tables by expiry. The clients index is partial
# because a client with no expiry is never a pruning candidate, and indexing
# those rows would cost writes to answer nothing; the other two columns are NOT
# NULL, so a partial index there would exclude nothing.
Index(
    "vault_oauth_clients_expires_at_idx",
    vault_oauth_clients.c.expires_at,
    postgresql_where=vault_oauth_clients.c.expires_at.is_not(None),
)
Index(
    "vault_oauth_pending_expires_at_idx",
    vault_oauth_pending_authorizations.c.expires_at,
)
Index(
    "vault_oauth_codes_expires_at_idx",
    vault_oauth_authorization_codes.c.expires_at,
)
Index(
    "vault_oauth_refresh_family_idx",
    vault_oauth_refresh_tokens.c.family_id,
)
Index(
    "vault_oauth_refresh_expires_at_idx",
    vault_oauth_refresh_tokens.c.expires_at,
)
