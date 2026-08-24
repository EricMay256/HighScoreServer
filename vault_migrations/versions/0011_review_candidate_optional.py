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

    Two states the old schema cannot represent, and **both** are checked before
    either constraint goes back on: a case whose candidate has been retired has
    no candidate id, and a candidate reviewed twice has two cases. Each is an
    ordinary product of this revision rather than corruption.

    The honest failure is to stop with a message naming the rows; the dishonest
    one would be to delete them so the constraints apply, which would discard
    the judgements this table exists to keep. Checking both up front is also
    what keeps the failure atomic -- validating only the first would restore
    NOT NULL and then fail on UNIQUE, leaving the schema half rolled back.
    """

    op.execute(
        """
        DO $$
        DECLARE
            orphans bigint;
            duplicated bigint;
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

            -- Both constraints came off, so both are checked before either
            -- goes back on. Dropping UNIQUE is what allows a candidate to be
            -- reviewed twice, so duplicates are the *expected* product of this
            -- revision -- and the ALTER below would meet them only after
            -- NOT NULL had already been restored, leaving the schema half
            -- rolled back. Refusing here keeps the failure atomic.
            SELECT count(*) INTO duplicated
            FROM (
                SELECT candidate_document_id
                FROM vault.vault_review_cases
                WHERE candidate_document_id IS NOT NULL
                GROUP BY candidate_document_id
                HAVING count(*) > 1
            ) AS repeats;

            IF duplicated > 0 THEN
                RAISE EXCEPTION
                    'cannot restore UNIQUE: % candidate(s) carry more than one '
                    'review case. Decide which judgement survives before '
                    'downgrading.',
                    duplicated;
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
