"""Schema-qualified SQLAlchemy Core metadata for vault persistence.

Alembic is the only production migration mechanism. This metadata exists for
query construction and schema-drift tests; application code must never call
``metadata.create_all()``.
"""

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    ARRAY,
    BigInteger,
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
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TSVECTOR, UUID


VAULT_SCHEMA = "vault"
EMBEDDING_DIMENSIONS = 1536

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
    Column(
        "status",
        document_status_enum,
        nullable=False,
        server_default=text("'active'"),
    ),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("body", Text, nullable=False),
    Column(
        "tags",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
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
    Column("embedding", VECTOR(EMBEDDING_DIMENSIONS)),
    Column("embedding_model", Text),
    Column("embedded_at", DateTime(timezone=True)),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english'::regconfig, "
            "coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english'::regconfig, "
            "coalesce(summary, '')), 'B') || "
            "setweight(to_tsvector('english'::regconfig, "
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
            ondelete="SET NULL",
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
    CheckConstraint(
        "(embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL) "
        "OR (embedding IS NOT NULL AND embedding_model IS NOT NULL "
        "AND embedded_at IS NOT NULL)",
        name="vault_documents_embedding_consistent",
    ),
    CheckConstraint(
        "(kind = 'note' AND compile_run_id IS NULL "
        "AND compiled_by IS NULL AND compiled_at IS NULL) "
        "OR (kind = 'wiki' AND compile_run_id IS NOT NULL "
        "AND compiled_by IS NOT NULL AND compiled_at IS NOT NULL)",
        name="vault_documents_compile_provenance_consistent",
    ),
)

vault_review_cases = Table(
    "vault_review_cases",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "candidate_document_id",
        Text,
        ForeignKey(
            "vault.vault_documents.id",
            name="vault_review_cases_candidate_document_id_fkey",
        ),
        nullable=False,
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
    UniqueConstraint(
        "candidate_document_id",
        name="vault_review_cases_candidate_document_id_key",
    ),
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
    CheckConstraint(
        "scopes <@ ARRAY['vault:read', 'vault:write', 'vault:review', "
        "'vault:compile', 'vault:export']::text[]",
        name="vault_agent_credentials_scopes_known",
    ),
)

vault_audit_events = Table(
    "vault_audit_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("principal_id", Text),
    Column("operation", Text, nullable=False),
    Column("target_id", Text),
    Column("outcome", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("trace_id", Text),
    Column("latency_ms", Double, nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    CheckConstraint(
        "btrim(operation) <> ''",
        name="vault_audit_events_operation_nonempty",
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
Index(
    "idx_vault_documents_embedding_hnsw",
    vault_documents.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=vault_documents.c.embedding.is_not(None),
)
Index(
    "idx_vault_documents_kind_status_updated",
    vault_documents.c.kind,
    vault_documents.c.status,
    vault_documents.c.updated_at.desc(),
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
Index(
    "idx_vault_compile_runs_state_started",
    vault_compile_runs.c.state,
    vault_compile_runs.c.started_at.desc(),
)
