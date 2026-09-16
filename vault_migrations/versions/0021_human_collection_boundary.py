"""The Human collection boundary: ownership, revisions, and a change feed.

Vault ADR 0049. Four things, none of which creates a Human note:

- ``vault_documents.collection`` says whose write path owns a row, and a CHECK
  ties it to the ``vault_path`` prefix. A stored column rather than one
  generated from the path, because a generated column would follow a move and
  change ownership with it; this one stays put, so a move across trees makes
  the pair disagree and the database refuses it.
- ``vault_documents.resource_revision`` moves on every readable change, where
  ``content_revision`` keeps its content-only contract for amendment
  proposals. Existing rows start at their ``content_revision``.
- ``vault_human_revisions`` and ``vault_human_changes``: snapshot history that
  outlives a deletion, and the ordered feed a sync client resumes from.
- Three Human scopes, widened into the scope CHECKs, plus CHECKs refusing any
  credential, grant, or refresh token that mixes a Human scope with an Agent
  mutation scope.

**The upgrade refuses a row outside ``Agent/``.** Every such row would have to
be classified, and both answers are wrong without a person deciding: ``agent``
makes it editable by Agent credentials, ``human`` enrolls it without the
preview ADR 0048 requires. The last recorded production census was Agent-only.

**It grants nothing**, per migration 0007: a migration reruns, and a grant in
one silently restores privilege on every rebuild. The Human scopes start
unheld.

**The downgrade refuses while Human data exists** -- a note, a revision, or a
feed entry -- for migration 0012's reason: removing authored content is a
deliberate act, not a side effect of a rollback. It does strip Human scopes
from credentials, grants, and refresh tokens, because the narrowed CHECKs would
reject rows carrying them; that can only ever reduce what a principal may do.

``starts_with`` rather than ``LIKE 'Agent/%'`` so no statement here carries a
``%`` through the driver's parameter handling.

Revision ID: 0021_human_collection_boundary
Revises: 0020_note_listing_sort_indexes
Create Date: 2026-09-12
"""

from alembic import op


revision = "0021_human_collection_boundary"
down_revision = "0020_note_listing_sort_indexes"
branch_labels = None
depends_on = None


# Spelled out rather than imported from `app.vault.constants`: historical
# revisions import only `vault_migrations.helpers`, so the lineage still loads
# after the runtime package moves. The runtime statements of these sets are
# `HUMAN_SCOPES` and `AGENT_MUTATION_SCOPES`.
_HUMAN_SCOPES = (
    "ARRAY['vault:human-read', 'vault:human-write', "
    "'vault:human-delete']::text[]"
)
_AGENT_MUTATION_SCOPES = (
    "ARRAY['vault:write', 'vault:propose', 'vault:update', "
    "'vault:delete', 'vault:review', 'vault:compile']::text[]"
)

_CREDENTIAL_SCOPES_BEFORE = (
    "ARRAY['vault:read', 'vault:write', 'vault:propose', 'vault:update', "
    "'vault:delete', 'vault:review', 'vault:compile', 'vault:export']::text[]"
)
_CREDENTIAL_SCOPES_AFTER = (
    "ARRAY['vault:read', 'vault:write', 'vault:propose', 'vault:update', "
    "'vault:delete', 'vault:review', 'vault:compile', 'vault:export', "
    "'vault:human-read', 'vault:human-write', 'vault:human-delete']::text[]"
)
_ENTITLEMENT_SCOPES_BEFORE = (
    "ARRAY['vault:update', 'vault:delete', 'vault:review', "
    "'vault:compile', 'vault:export']::text[]"
)
_ENTITLEMENT_SCOPES_AFTER = (
    "ARRAY['vault:update', 'vault:delete', 'vault:review', "
    "'vault:compile', 'vault:export', 'vault:human-read', "
    "'vault:human-write', 'vault:human-delete']::text[]"
)


def _exclusive(scopes_expression: str) -> str:
    return (
        f"NOT (({scopes_expression}) && {_HUMAN_SCOPES} "
        f"AND ({scopes_expression}) && {_AGENT_MUTATION_SCOPES})"
    )


def _without_human_scopes(column: str) -> str:
    return (
        f"ARRAY(SELECT scope FROM unnest({column}) AS scope "
        f"WHERE NOT scope = ANY ({_HUMAN_SCOPES}) ORDER BY scope)"
    )


def upgrade() -> None:
    # First, so a refusal leaves 0020 exactly as it was.
    op.execute(
        """
        DO $$
        DECLARE
            unowned bigint;
        BEGIN
            SELECT count(*) INTO unowned
            FROM vault.vault_documents
            WHERE NOT starts_with(vault_path, 'Agent/');

            IF unowned > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'cannot assign ownership: ' || unowned
                    || ' document(s) sit outside Agent/. Classify or remove '
                    'them deliberately before this revision; see vault ADR 0049.';
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE TYPE vault.vault_document_collection AS ENUM ('agent', 'human');

        ALTER TABLE vault.vault_documents
            ADD COLUMN resource_revision bigint NOT NULL DEFAULT 1,
            ADD COLUMN collection vault.vault_document_collection
                NOT NULL DEFAULT 'agent';

        -- Every content change so far was also a resource change, so the
        -- resource revision starts where the content revision stands.
        UPDATE vault.vault_documents
        SET resource_revision = content_revision
        WHERE resource_revision <> content_revision;

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_resource_revision_covers_content
                CHECK (resource_revision >= content_revision),
            ADD CONSTRAINT vault_documents_collection_matches_path
                CHECK (
                    (collection = 'agent' AND starts_with(vault_path, 'Agent/'))
                    OR (collection = 'human' AND starts_with(vault_path, 'Human/'))
                ),
            ADD CONSTRAINT vault_documents_human_has_no_agent_workflow
                CHECK (
                    collection = 'agent'
                    OR (
                        kind = 'note'
                        AND promotion_status IS NULL
                        AND compile_declined_at IS NULL
                    )
                );

        CREATE TABLE vault.vault_human_revisions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id text NOT NULL,
            resource_revision bigint NOT NULL,
            content_revision bigint NOT NULL,
            operation text NOT NULL,
            vault_path text NOT NULL,
            doc_type text,
            doc_status text,
            title text NOT NULL,
            summary text,
            body text NOT NULL,
            tags text[] NOT NULL DEFAULT '{}'::text[],
            aliases text[] NOT NULL DEFAULT '{}'::text[],
            frontmatter jsonb NOT NULL DEFAULT '{}'::jsonb,
            facets jsonb NOT NULL DEFAULT '{}'::jsonb,
            related_ids text[] NOT NULL DEFAULT '{}'::text[],
            source_ids text[] NOT NULL DEFAULT '{}'::text[],
            source_url text,
            principal_id text NOT NULL,
            request_id text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT vault_human_revisions_operation_known
                CHECK (operation IN ('create', 'edit', 'move', 'delete', 'restore')),
            CONSTRAINT vault_human_revisions_revisions_positive
                CHECK (resource_revision > 0 AND content_revision > 0),
            CONSTRAINT vault_human_revisions_path_is_human
                CHECK (starts_with(vault_path, 'Human/')),
            CONSTRAINT vault_human_revisions_principal_nonempty
                CHECK (btrim(principal_id) <> ''),
            CONSTRAINT vault_human_revisions_request_id_nonempty
                CHECK (btrim(request_id) <> ''),
            CONSTRAINT vault_human_revisions_document_revision_key
                UNIQUE (document_id, resource_revision)
        );

        CREATE TABLE vault.vault_human_changes (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id text NOT NULL,
            resource_revision bigint NOT NULL,
            change_kind text NOT NULL,
            vault_path text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT vault_human_changes_kind_known
                CHECK (change_kind IN ('upsert', 'delete')),
            CONSTRAINT vault_human_changes_revision_positive
                CHECK (resource_revision > 0),
            CONSTRAINT vault_human_changes_path_is_human
                CHECK (starts_with(vault_path, 'Human/')),
            CONSTRAINT vault_human_changes_document_revision_key
                UNIQUE (document_id, resource_revision)
        );
        """
    )

    op.execute(
        f"""
        ALTER TABLE vault.vault_agent_credentials
            DROP CONSTRAINT vault_agent_credentials_scopes_known,
            ADD CONSTRAINT vault_agent_credentials_scopes_known
                CHECK (scopes <@ {_CREDENTIAL_SCOPES_AFTER}),
            ADD CONSTRAINT vault_agent_credentials_human_agent_exclusive
                CHECK ({_exclusive("scopes")});

        ALTER TABLE vault.vault_oauth_refresh_tokens
            DROP CONSTRAINT vault_oauth_refresh_scopes_known,
            ADD CONSTRAINT vault_oauth_refresh_scopes_known
                CHECK (scopes <@ {_CREDENTIAL_SCOPES_AFTER}),
            ADD CONSTRAINT vault_oauth_refresh_human_agent_exclusive
                CHECK ({_exclusive("scopes")});

        ALTER TABLE vault.vault_oauth_grants
            DROP CONSTRAINT vault_oauth_grants_entitled_scopes_privileged,
            ADD CONSTRAINT vault_oauth_grants_entitled_scopes_privileged
                CHECK (entitled_scopes <@ {_ENTITLEMENT_SCOPES_AFTER}),
            ADD CONSTRAINT vault_oauth_grants_human_agent_exclusive
                CHECK ({_exclusive("authorized_scopes || entitled_scopes")});
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM vault.vault_documents WHERE collection = 'human'
            )
            OR EXISTS (SELECT 1 FROM vault.vault_human_revisions)
            OR EXISTS (SELECT 1 FROM vault.vault_human_changes)
            THEN
                RAISE EXCEPTION USING MESSAGE =
                    'cannot remove the Human collection: Human notes, revisions, '
                    'or feed entries exist. Removing them is a deliberate act, '
                    'not a side effect of a rollback; see vault ADR 0049.';
            END IF;
        END
        $$;
        """
    )

    op.execute(
        f"""
        UPDATE vault.vault_agent_credentials
        SET scopes = {_without_human_scopes("scopes")}
        WHERE scopes && {_HUMAN_SCOPES};

        UPDATE vault.vault_oauth_refresh_tokens
        SET scopes = {_without_human_scopes("scopes")}
        WHERE scopes && {_HUMAN_SCOPES};

        UPDATE vault.vault_oauth_grants
        SET entitled_scopes = {_without_human_scopes("entitled_scopes")}
        WHERE entitled_scopes && {_HUMAN_SCOPES};

        ALTER TABLE vault.vault_agent_credentials
            DROP CONSTRAINT vault_agent_credentials_human_agent_exclusive,
            DROP CONSTRAINT vault_agent_credentials_scopes_known,
            ADD CONSTRAINT vault_agent_credentials_scopes_known
                CHECK (scopes <@ {_CREDENTIAL_SCOPES_BEFORE});

        ALTER TABLE vault.vault_oauth_refresh_tokens
            DROP CONSTRAINT vault_oauth_refresh_human_agent_exclusive,
            DROP CONSTRAINT vault_oauth_refresh_scopes_known,
            ADD CONSTRAINT vault_oauth_refresh_scopes_known
                CHECK (scopes <@ {_CREDENTIAL_SCOPES_BEFORE});

        ALTER TABLE vault.vault_oauth_grants
            DROP CONSTRAINT vault_oauth_grants_human_agent_exclusive,
            DROP CONSTRAINT vault_oauth_grants_entitled_scopes_privileged,
            ADD CONSTRAINT vault_oauth_grants_entitled_scopes_privileged
                CHECK (entitled_scopes <@ {_ENTITLEMENT_SCOPES_BEFORE});
        """
    )

    op.execute(
        """
        DROP TABLE vault.vault_human_changes;
        DROP TABLE vault.vault_human_revisions;

        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT vault_documents_human_has_no_agent_workflow,
            DROP CONSTRAINT vault_documents_collection_matches_path,
            DROP CONSTRAINT vault_documents_resource_revision_covers_content,
            DROP COLUMN collection,
            DROP COLUMN resource_revision;

        DROP TYPE vault.vault_document_collection;
        """
    )
