"""document origin: upstream provenance for replayed content

``vault_documents.origin`` records who wrote a note, when, and under what run,
for content that existed before it was contributed here. The existing columns
cannot carry those facts and must not be made to: ``contributed_by`` comes from
the credential rather than the body (ADR 0016), so on an import it names the
importer, and ``created_at`` is when the row landed, so backdating it would make
the write ledger disagree with itself.

The 2026-08-12 corpus migration is the evidence. Replaying 49 Stage-A notes
through the contribution endpoint dropped ``ContributedBy`` (two distinct real
authors, ``agent:claude-code`` and ``agent:codex``, collapsed to
``agent:importer``), ``CreatedAt``/``LastUpdated``, ``Source`` (33 of 61 notes
carried one, and 32 of those are prose rather than a URL, so ``source_url``'s
``AnyUrl`` refused them), and ``ClientRunID`` (57 of 61). None of it was
recoverable from the database.

One JSONB column rather than five typed ones, following ``facets`` in revision
0005: the CHECK constrains **shape only** -- an object of non-blank strings --
and which keys are legal lives in ``app/vault/origin.py`` at the write boundary,
so a sixth provenance fact stays a data change rather than a migration (ADR
0009's precedent). The case is stronger here than for facets: origin is
bookkeeping that gets projected into frontmatter and is never filtered on, so
there is not even a containment query asking for its own index.

Timestamps are stored as ISO-8601 *text*, not timestamptz. They are governance
values the export re-emits verbatim rather than instants the vault computes
with, and text is what makes that re-emission byte-identical to the upstream
note. Their shape is checked in application code alongside the key vocabulary.

**No REQUEST_DIGEST_VERSION bump.** ``canonical_request_digest`` dumps with
``exclude_unset=True`` since migration 0006 and ADR 0016's amendment, precisely
so an additive optional field is a non-event: a request that does not mention
``origin`` serializes exactly as it did before, and its stored digest stays
comparable. Verified against fixed sample payloads before and after this change,
not assumed.

Revision ID: 0010_document_origin
Revises: 0009_request_digest_v3
Create Date: 2026-08-20
"""

from alembic import op


revision = "0010_document_origin"
down_revision = "0009_request_digest_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN origin JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )

    # Shape only, and strict about it for the reason revision 0005 gives about
    # facets: accepting both a scalar and something else would mean every reader
    # handles both. An origin map is an object whose every value is a non-blank
    # string.
    #
    # In a function because PostgreSQL refuses a subquery inside a CHECK, and
    # walking a JSONB object's entries needs jsonb_each -- a set-returning
    # function, hence a subquery. Same workaround as vault.jsonb_is_facet_map,
    # and the same obligation: IMMUTABLE is asserted here, so the body must
    # depend on nothing but its argument. It does.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION vault.jsonb_is_origin_map(value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT jsonb_typeof(value) = 'object'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_each(value) AS entry(key, item)
                   WHERE jsonb_typeof(item) <> 'string'
                      OR btrim(item #>> '{}') = ''
               );
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_origin_shape
                CHECK (vault.jsonb_is_origin_map(origin));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT vault_documents_origin_shape;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS vault.jsonb_is_origin_map(jsonb);")
    op.execute("ALTER TABLE vault.vault_documents DROP COLUMN origin;")
