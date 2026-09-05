"""Indexes for the note listing's recency orders.

`/notes` can now be read newest-first by `updated_at` or `created_at` as well
as by `vault_path` (ADR 0045). Each order pages by a compound keyset on
`(key, id)`, so what it asks of Postgres is an ordered walk of a filtered set
with a LIMIT -- exactly the shape an index can serve end to end, and exactly
the shape that degrades into a full sort of the corpus without one.

**Ascending, not descending, though the listing reads newest first.** A btree
scans either way, and `(updated_at, id)` reversed is precisely
`ORDER BY updated_at DESC, id DESC` -- the ordering the listing asks for. What
cannot be reversed as a unit is a *mixed* index, so declaring the key DESC and
the tiebreaker ASC would have served the query it was written for and nothing
else. Ascending keeps the oldest-first variants ADR 0045 defers available for
free.

**No leading `status` column, which the plan called for and this does not.**
The listing always filters `status IN ('active', 'archived')` -- two of three
values, so nearly every row. Leading with it buys almost no selectivity while
putting a non-equality in front of the ordering key, which is what stops
Postgres walking the index in order: it would gather matching rows and sort
them, which is the cost this index exists to avoid. Leading with the timestamp
lets the planner walk in order and drop flagged rows as it goes.

The read policy's path predicate and any tag or facet filter are applied the
same way, as filters on an ordered walk. A page is at most 200 rows, so the
walk stops early even where the filters are selective.

`vault_path` needs nothing new: `vault_documents_vault_path_key` is UNIQUE and
already orders the default listing.

Two more indexes to maintain on every write to `vault_documents`. The corpus
is small enough today that the planner might reasonably choose a sort instead;
they are added anyway because a listing whose cost grows with the corpus is a
listing that stops working exactly when there is enough in it to be worth
browsing.

Revision ID: 0020_note_listing_sort_indexes
Revises: 0019_oauth_grant_label
Create Date: 2026-09-05
"""

from alembic import op


revision = "0020_note_listing_sort_indexes"
down_revision = "0019_oauth_grant_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `(timestamp, id)` in that order and both ascending: the pair is the
    # keyset the listing pages on, and the id is what makes the order total
    # when notes written in one transaction share a timestamp exactly.
    op.execute(
        """
        CREATE INDEX idx_vault_documents_updated_at_id
            ON vault.vault_documents (updated_at, id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_vault_documents_created_at_id
            ON vault.vault_documents (created_at, id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS vault.idx_vault_documents_updated_at_id")
    op.execute("DROP INDEX IF EXISTS vault.idx_vault_documents_created_at_id")
