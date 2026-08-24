"""document promotion_status

Promotion candidacy becomes a field on the document, per vault ADR 0023. The
folder ``Agent/Promotion Candidates/`` stops being a place a human drops a file
and becomes a projection of this column: the exporter writes a candidate there
because the row's ``vault_path`` says so, and dragging a file in by hand does
nothing because the row still names its own path.

**Nullable, with no default.** Absence means "never proposed", which is the
state almost every note is in and is not the same as ``retracted`` -- considered
and declined. Backfilling a default would assert that 70 notes had been through
a judgement none of them has had.

**An enum, in the shape ``vault_review_state`` already uses.** The three values
are a closed vocabulary set by a reviewer holding ``vault:review``, not a
governance one that evolves in ``types.yml``; that is the distinction ADR 0009
draws when it keeps ``doc_type`` TEXT, and it points the other way here.

Routing is binary -- candidate or not -- while the field is three-valued so the
*outcome* is recorded. ``promoted`` and ``retracted`` both export back to
``Agent/notes/`` and both mean "settled", in the same way ``accepted`` and
``rejected`` do for a review case. That is what stops a note being re-proposed
forever and lets a reviewer see that something was already considered.

``promotion_status`` is a third, independent field alongside ``status`` (the
vault's visibility gate) and ``doc_status`` (the Status Map value). ADR 0011's
reason applies unchanged: three different questions, three different columns,
none derived from the others.

No index. The only query today is the export, which walks by ``vault_path``
prefix and never filters on this column; a "list the candidates" surface arrives
with the admin MCP and can bring its own index if it needs one.

The revision id is kept under 32 characters because
``vault_alembic_version.version_num`` is ``varchar(32)`` -- a longer one fails
at the very end of the upgrade, after the DDL has run.

Revision ID: 0012_document_promotion_status
Revises: 0011_review_candidate_optional
Create Date: 2026-08-21
"""

from alembic import op


revision = "0012_document_promotion_status"
down_revision = "0011_review_candidate_optional"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE vault.vault_promotion_status
            AS ENUM ('candidate', 'promoted', 'retracted');

        ALTER TABLE vault.vault_documents
            ADD COLUMN promotion_status vault.vault_promotion_status;
        """
    )


def downgrade() -> None:
    """Refuse rather than silently discard a settled judgement.

    A non-null ``promotion_status`` records that a person looked at a note and
    decided something about it. The old schema cannot hold that, and dropping
    the column would destroy it without saying so -- the same objection
    migration 0011's downgrade raises about review cases whose candidate is
    gone. ``candidate`` rows matter twice over: they also sit at a
    ``vault_path`` this schema has no way to explain.

    An operator who really wants the old shape must clear the column first,
    which is a deliberate act rather than a side effect of a rollback.
    """

    op.execute(
        """
        DO $$
        DECLARE
            settled bigint;
        BEGIN
            SELECT count(*) INTO settled
            FROM vault.vault_documents
            WHERE promotion_status IS NOT NULL;

            IF settled > 0 THEN
                RAISE EXCEPTION
                    'cannot drop promotion_status: % document(s) carry a '
                    'promotion judgement. Clear the column before downgrading.',
                    settled;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP COLUMN IF EXISTS promotion_status;
        DROP TYPE IF EXISTS vault.vault_promotion_status;
        """
    )
