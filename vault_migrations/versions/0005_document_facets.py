"""document facets

``vault_documents.facets`` carries classification that relates notes to each
other -- project, area, system -- without entering the embedding text. See vault
ADR 0017.

The column exists rather than a reserved prefix inside ``tags`` because ADR 0013
puts ``tags`` in the embedding text, and that turns out to be decisive rather
than untidy. Measured on ten real corpus documents, adding one shared tag raised
*every* pairwise cosine: mean +0.0385, max +0.0825. The dedup calibration in ADR
0016's amendment has a margin of 0.0072 between the known-distinct floor
(0.7406) and the known-duplicate ceiling (0.7478), so that inflation is 5.3x the
whole margin and would invert the separation. A column the embedding assembler
never reads makes the exclusion structural instead of a strip rule.

Shape is constrained here; vocabulary is not. Which projects exist is checked in
application code against the governance YAML, following ADR 0009 -- adding a
project stays a data change rather than a migration.

The GIN index is jsonb_path_ops rather than the default jsonb_ops: it indexes
only containment (@>), which is the sole operator a facet filter needs, and is
substantially smaller and faster for it. Existence operators (?, ?|, ?&) are not
supported by it -- a query needing those wants a different index, not a widened
one.

Revision ID: 0005_document_facets
Revises: 0004_reconciliation
Create Date: 2026-08-12
"""

from alembic import op


revision = "0005_document_facets"
down_revision = "0004_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN facets JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )

    # Shape only, and deliberately strict about it: a facet map is an object
    # whose every value is an array of non-blank strings. Scalars are refused
    # rather than coerced, because accepting both {"project": "hss"} and
    # {"project": ["hss"]} would mean every reader has to handle both, and a
    # containment query written for one silently misses the other.
    #
    # This lives in a function because PostgreSQL rejects a subquery inside a
    # CHECK constraint outright ("cannot use subquery in check constraint"),
    # and walking a JSONB object's entries requires jsonb_each -- a set
    # returning function, hence a subquery. Same shape of workaround as
    # vault.text_array_to_string in revision 0004, and the same obligation:
    # IMMUTABLE is asserted by us, so the body must depend on nothing but its
    # argument. It does.
    #
    # The 64-character name ceiling is MAX_FACET_NAME_LENGTH in
    # app/vault/constants.py. Restated rather than interpolated because a
    # migration must keep describing the DDL it actually ran.
    op.execute(
        """
        CREATE FUNCTION vault.jsonb_is_facet_map(value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$
            SELECT jsonb_typeof(value) = 'object'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_each(value) AS entry(key, val)
                   WHERE jsonb_typeof(entry.val) <> 'array'
                      OR btrim(entry.key) = ''
                      OR length(entry.key) > 64
                      OR EXISTS (
                           SELECT 1
                           FROM jsonb_array_elements(entry.val) AS item
                           WHERE jsonb_typeof(item) <> 'string'
                              OR btrim(item #>> '{}') = ''
                      )
               )
        $$;
        """
    )

    # An empty array is allowed by the function above: a facet named and left
    # empty is distinguishable from an absent one. Application code normalizes
    # those away before they reach here, so the database is the second layer
    # rather than the only one.
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_facets_shape
            CHECK (vault.jsonb_is_facet_map(facets));
        """
    )

    op.execute(
        """
        CREATE INDEX idx_vault_documents_facets
            ON vault.vault_documents USING gin (facets jsonb_path_ops);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS vault.idx_vault_documents_facets;
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_facets_shape;
        ALTER TABLE vault.vault_documents DROP COLUMN IF EXISTS facets;
        DROP FUNCTION IF EXISTS vault.jsonb_is_facet_map(jsonb);
        """
    )
