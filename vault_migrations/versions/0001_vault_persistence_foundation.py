"""vault persistence foundation

Creates the isolated vault schema objects for the initial shared-database
topology. This revision contains schema only: no corpus data, credentials,
embedding-provider calls, or imports.

Revision ID: 0001_vault_foundation
Revises:
Create Date: 2026-07-25
"""

from alembic import op


revision = "0001_vault_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS vault")

    op.execute(
        """
        CREATE TYPE vault.vault_document_kind AS ENUM ('note', 'wiki');
        CREATE TYPE vault.vault_document_status
            AS ENUM ('active', 'flagged', 'archived');
        CREATE TYPE vault.vault_review_state
            AS ENUM ('pending', 'accepted', 'rejected', 'superseded');
        CREATE TYPE vault.vault_write_request_state
            AS ENUM ('processing', 'inserted', 'flagged', 'invalid', 'failed');
        CREATE TYPE vault.vault_compile_run_state
            AS ENUM ('running', 'succeeded', 'failed');
        """
    )

    op.execute(
        """
        CREATE TABLE vault.vault_compile_runs (
            id uuid NOT NULL,
            compiler_principal_id text NOT NULL,
            state vault.vault_compile_run_state
                DEFAULT 'running' NOT NULL,
            started_at timestamptz DEFAULT now() NOT NULL,
            completed_at timestamptz,
            input_frontier jsonb DEFAULT '{}'::jsonb NOT NULL,
            output_frontier jsonb DEFAULT '{}'::jsonb NOT NULL,
            error_summary text,
            CONSTRAINT vault_compile_runs_pkey PRIMARY KEY (id),
            CONSTRAINT vault_compile_runs_principal_nonempty
                CHECK (btrim(compiler_principal_id) <> ''),
            CONSTRAINT vault_compile_runs_completion_consistent CHECK (
                (state = 'running' AND completed_at IS NULL)
                OR (state <> 'running' AND completed_at IS NOT NULL)
            )
        );

        CREATE TABLE vault.vault_documents (
            id text NOT NULL,
            kind vault.vault_document_kind DEFAULT 'note' NOT NULL,
            status vault.vault_document_status DEFAULT 'active' NOT NULL,
            title text NOT NULL,
            summary text,
            body text NOT NULL,
            tags text[] DEFAULT '{}'::text[] NOT NULL,
            related_ids text[] DEFAULT '{}'::text[] NOT NULL,
            source_ids text[] DEFAULT '{}'::text[] NOT NULL,
            contributed_by text NOT NULL,
            source_url text,
            provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
            schema_version integer NOT NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            updated_at timestamptz DEFAULT now() NOT NULL,
            embedding vector(1536),
            embedding_model text,
            embedded_at timestamptz,
            search_vector tsvector GENERATED ALWAYS AS (
                setweight(
                    to_tsvector('english'::regconfig, coalesce(title, '')),
                    'A'
                )
                || setweight(
                    to_tsvector('english'::regconfig, coalesce(summary, '')),
                    'B'
                )
                || setweight(
                    to_tsvector('english'::regconfig, coalesce(body, '')),
                    'C'
                )
            ) STORED NOT NULL,
            compile_run_id uuid,
            compiled_by text,
            compiled_at timestamptz,
            CONSTRAINT vault_documents_pkey PRIMARY KEY (id),
            CONSTRAINT vault_documents_compile_run_id_fkey
                FOREIGN KEY (compile_run_id)
                REFERENCES vault.vault_compile_runs(id) ON DELETE SET NULL,
            CONSTRAINT vault_documents_id_nonempty
                CHECK (btrim(id) <> ''),
            CONSTRAINT vault_documents_title_nonempty
                CHECK (btrim(title) <> ''),
            CONSTRAINT vault_documents_body_nonempty
                CHECK (btrim(body) <> ''),
            CONSTRAINT vault_documents_contributor_nonempty
                CHECK (btrim(contributed_by) <> ''),
            CONSTRAINT vault_documents_schema_version_positive
                CHECK (schema_version > 0),
            CONSTRAINT vault_documents_embedding_consistent CHECK (
                (
                    embedding IS NULL
                    AND embedding_model IS NULL
                    AND embedded_at IS NULL
                )
                OR (
                    embedding IS NOT NULL
                    AND embedding_model IS NOT NULL
                    AND embedded_at IS NOT NULL
                )
            ),
            CONSTRAINT vault_documents_compile_provenance_consistent CHECK (
                (
                    kind = 'note'
                    AND compile_run_id IS NULL
                    AND compiled_by IS NULL
                    AND compiled_at IS NULL
                )
                OR (
                    kind = 'wiki'
                    AND compile_run_id IS NOT NULL
                    AND compiled_by IS NOT NULL
                    AND compiled_at IS NOT NULL
                )
            )
        );

        CREATE TABLE vault.vault_review_cases (
            id uuid NOT NULL,
            candidate_document_id text NOT NULL,
            state vault.vault_review_state DEFAULT 'pending' NOT NULL,
            reason text NOT NULL,
            similar_documents jsonb DEFAULT '[]'::jsonb NOT NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            decided_at timestamptz,
            decided_by text,
            decision_note text,
            CONSTRAINT vault_review_cases_pkey PRIMARY KEY (id),
            CONSTRAINT vault_review_cases_candidate_document_id_key
                UNIQUE (candidate_document_id),
            CONSTRAINT vault_review_cases_candidate_document_id_fkey
                FOREIGN KEY (candidate_document_id)
                REFERENCES vault.vault_documents(id),
            CONSTRAINT vault_review_cases_reason_nonempty
                CHECK (btrim(reason) <> ''),
            CONSTRAINT vault_review_cases_decision_consistent CHECK (
                (
                    state = 'pending'
                    AND decided_at IS NULL
                    AND decided_by IS NULL
                )
                OR (
                    state <> 'pending'
                    AND decided_at IS NOT NULL
                    AND decided_by IS NOT NULL
                )
            )
        );

        CREATE TABLE vault.vault_write_requests (
            principal_id text NOT NULL,
            idempotency_key text NOT NULL,
            request_sha256 bytea NOT NULL,
            state vault.vault_write_request_state
                DEFAULT 'processing' NOT NULL,
            document_id text,
            response jsonb,
            created_at timestamptz DEFAULT now() NOT NULL,
            completed_at timestamptz,
            CONSTRAINT vault_write_requests_pkey
                PRIMARY KEY (principal_id, idempotency_key),
            CONSTRAINT vault_write_requests_document_id_fkey
                FOREIGN KEY (document_id)
                REFERENCES vault.vault_documents(id),
            CONSTRAINT vault_write_requests_principal_nonempty
                CHECK (btrim(principal_id) <> ''),
            CONSTRAINT vault_write_requests_idempotency_key_format
                CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'),
            CONSTRAINT vault_write_requests_sha256_length
                CHECK (octet_length(request_sha256) = 32),
            CONSTRAINT vault_write_requests_completion_consistent CHECK (
                (state = 'processing' AND completed_at IS NULL)
                OR (state <> 'processing' AND completed_at IS NOT NULL)
            )
        );

        CREATE TABLE vault.vault_agent_credentials (
            id text NOT NULL,
            principal_id text NOT NULL,
            display_name text NOT NULL,
            secret_sha256 bytea NOT NULL,
            scopes text[] DEFAULT '{}'::text[] NOT NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            expires_at timestamptz,
            revoked_at timestamptz,
            last_used_at timestamptz,
            CONSTRAINT vault_agent_credentials_pkey PRIMARY KEY (id),
            CONSTRAINT vault_agent_credentials_id_format
                CHECK (id ~ '^[A-Za-z0-9_-]{8,64}$'),
            CONSTRAINT vault_agent_credentials_principal_nonempty
                CHECK (btrim(principal_id) <> ''),
            CONSTRAINT vault_agent_credentials_display_name_nonempty
                CHECK (btrim(display_name) <> ''),
            CONSTRAINT vault_agent_credentials_sha256_length
                CHECK (octet_length(secret_sha256) = 32),
            CONSTRAINT vault_agent_credentials_scopes_known CHECK (
                scopes <@ ARRAY[
                    'vault:read',
                    'vault:write',
                    'vault:review',
                    'vault:compile',
                    'vault:export'
                ]::text[]
            )
        );

        CREATE TABLE vault.vault_audit_events (
            id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
            principal_id text,
            operation text NOT NULL,
            target_id text,
            outcome text NOT NULL,
            request_id text NOT NULL,
            trace_id text,
            latency_ms double precision NOT NULL,
            occurred_at timestamptz DEFAULT now() NOT NULL,
            CONSTRAINT vault_audit_events_pkey PRIMARY KEY (id),
            CONSTRAINT vault_audit_events_operation_nonempty
                CHECK (btrim(operation) <> ''),
            CONSTRAINT vault_audit_events_outcome_nonempty
                CHECK (btrim(outcome) <> ''),
            CONSTRAINT vault_audit_events_request_id_nonempty
                CHECK (btrim(request_id) <> ''),
            CONSTRAINT vault_audit_events_latency_nonnegative
                CHECK (latency_ms >= 0)
        );
        """
    )

    op.execute(
        """
        CREATE INDEX idx_vault_documents_search_vector
            ON vault.vault_documents USING gin (search_vector);
        CREATE INDEX idx_vault_documents_tags
            ON vault.vault_documents USING gin (tags);
        CREATE INDEX idx_vault_documents_embedding_hnsw
            ON vault.vault_documents
            USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL;
        CREATE INDEX idx_vault_documents_kind_status_updated
            ON vault.vault_documents
            USING btree (kind, status, updated_at DESC);
        CREATE INDEX idx_vault_review_cases_state_created
            ON vault.vault_review_cases USING btree (state, created_at);
        CREATE INDEX idx_vault_write_requests_created
            ON vault.vault_write_requests USING btree (created_at);
        CREATE INDEX idx_vault_agent_credentials_principal
            ON vault.vault_agent_credentials USING btree (principal_id);
        CREATE INDEX idx_vault_audit_events_principal_occurred
            ON vault.vault_audit_events
            USING btree (principal_id, occurred_at DESC);
        CREATE INDEX idx_vault_compile_runs_state_started
            ON vault.vault_compile_runs
            USING btree (state, started_at DESC);
        """
    )


def downgrade() -> None:
    # Local/test rollback only. The database-wide vector extension is retained
    # because other schemas may depend on it.
    op.execute(
        """
        DROP TABLE IF EXISTS vault.vault_audit_events;
        DROP TABLE IF EXISTS vault.vault_agent_credentials;
        DROP TABLE IF EXISTS vault.vault_write_requests;
        DROP TABLE IF EXISTS vault.vault_review_cases;
        DROP TABLE IF EXISTS vault.vault_documents;
        DROP TABLE IF EXISTS vault.vault_compile_runs;

        DROP TYPE IF EXISTS vault.vault_compile_run_state;
        DROP TYPE IF EXISTS vault.vault_write_request_state;
        DROP TYPE IF EXISTS vault.vault_review_state;
        DROP TYPE IF EXISTS vault.vault_document_status;
        DROP TYPE IF EXISTS vault.vault_document_kind;
        """
    )
