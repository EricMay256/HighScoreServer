"""a review candidate may be deleted, and may be reviewed more than once

Two constraints on ``vault_review_cases.candidate_document_id`` come off, for
two different reasons.

**NOT NULL comes off so a flagged note can be retired.** ADR 0019 made the
candidate reference block retirement in *every* review state, on the reasoning
that a resolved case would otherwise pass the service check and fail later at
the foreign key. The consequence was not noticed at the time: a note that is
flagged once can never be deleted, because nothing settles a case and even a
settled one keeps blocking. That is a data-protection problem the day a flagged
note contains a secret, and it was hit in practice by the load probe, whose own
duplicate notes flagged and then could not be cleaned up.

The fix is the pattern this schema already uses one table over.
``vault_write_requests.document_id`` is nullable precisely so the ledger row can
outlive its subject and state what is true -- "this key was used, and what it
produced is gone". A review case says the same kind of thing: the judgement is
the durable record, and the pointer to what was judged is allowed to dangle.

**UNIQUE comes off so a case can be re-opened.** One case per candidate forever
means a wrong decision can only be corrected by overwriting the record of the
first one, which destroys exactly the history the table exists to keep. Nothing
in application code depended on the uniqueness -- the only reference is the
retirement-blocking count, which is a ``WHERE`` rather than a lookup.

The two are independent. Postgres treats NULLs as distinct, so nullability alone
would not have forced the unique constraint off.

The revision id is kept under 32 characters because
``vault_alembic_version.version_num`` is ``varchar(32)`` -- a longer one fails
at the very end of the upgrade, after the DDL has run.

Revision ID: 0011_review_candidate_optional
Revises: 0010_document_origin
Create Date: 2026-08-21
"""

from alembic import op


revision = "0011_review_candidate_optional"
down_revision = "0010_document_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_review_cases
            DROP CONSTRAINT vault_review_cases_candidate_document_id_key;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_review_cases
            ALTER COLUMN candidate_document_id DROP NOT NULL;
        """
    )


def downgrade() -> None:
    """Restore both constraints, and refuse rather than destroy evidence.

    A case whose candidate has been retired cannot be represented in the old
    schema. The honest failure is to stop with a message naming the rows; the
    dishonest one would be to delete them so the constraint applies, which
    would discard the judgements this table exists to keep. An operator who
    really wants the old shape must decide what happens to those rows first.
    """

    op.execute(
        """
        DO $$
        DECLARE
            orphans bigint;
        BEGIN
            SELECT count(*) INTO orphans
            FROM vault.vault_review_cases
            WHERE candidate_document_id IS NULL;

            IF orphans > 0 THEN
                RAISE EXCEPTION
                    'cannot restore NOT NULL: % review case(s) name a retired '
                    'candidate. Decide what happens to them before downgrading.',
                    orphans;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_review_cases
            ALTER COLUMN candidate_document_id SET NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_review_cases
            ADD CONSTRAINT vault_review_cases_candidate_document_id_key
                UNIQUE (candidate_document_id);
        """
    )
