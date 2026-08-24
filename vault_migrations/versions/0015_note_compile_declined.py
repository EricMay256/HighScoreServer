"""note compile_declined_at

Declining a note becomes a fact on the note, per vault ADR 0027's 2026-08-24
amendment. It replaces the compile **frontier** as the thing that stops the plan
becoming a permanent backlog, and it replaces it because a timestamp cannot say
what a judgement says.

**The frontier conflated two different states.** It recorded "the source corpus
had moved this far when the run was planned", and the next plan offered only
notes newer than that. So "considered and declined" and "never offered at all"
were indistinguishable -- and the second one really happened, through the
ordinary review workflow rather than through any misbehaviour:

1. a note is contributed and the dedup gate flags it;
2. a compile run is planned. The note is *excluded* from the plan, because a
   flagged note is never offered as a new source -- but it still counts toward
   ``note_frontier``, which is ``max(updated_at)`` across every note whatever
   its status;
3. the run succeeds and publishes that frontier;
4. a reviewer approves the note. ``set_status`` deliberately does not move
   ``updated_at`` -- adjudicating a note is not editing it, and the export would
   otherwise churn every reviewed file;
5. the note is now active, uncovered, and permanently below the frontier. No
   incremental plan will ever offer it again.

Marking the decline instead cannot express that, because only a note somebody
actually declined is marked. A note nobody was shown stays unmarked and keeps
being offered.

**Nullable, no default, and no backfill.** Absence means "not declined", which
is the state every note is in and must remain in -- backfilling anything would
assert a judgement that was never made, and would silently hide exactly the
notes this migration exists to surface. The frontier's own history stays on
``vault_compile_runs.output_frontier``; it is simply no longer read when
planning.

**A decline expires when the note changes.** Not enforced here, because it is a
comparison rather than a constraint: the planner treats a decline as stale when
``updated_at > compile_declined_at``. The frontier gave that behaviour for free
by construction; stating it explicitly is better than inheriting it.

**No run reference.** The obvious extra column -- which run declined this --
would be a foreign key from a note to a compile run, and two things argue
against it. ``vault_documents_compile_provenance_consistent`` already says a
note carries no ``compile_run_id``: compile provenance belongs to pages. And
runs are cited ``ON DELETE RESTRICT``, so a run that wrote no pages but declined
twenty notes would become unprunable -- exactly the run ADR 0027 expects pruning
to reach. Who declined and when is the audit event's job (ADR 0002), and a run
is a time window, so ``compile_declined_at`` supports a range revert anyway.

The kind CHECK follows ``compile_provenance_consistent`` rather than
``promotion_status``, which has none. Declining is refusing to write a page
*from a note*; there is no reading under which a wiki page is declined, so the
schema says so.

No index. The planner already loads every note's state in one query to compute
coverage, and it reads this column from those same rows.

The revision id is kept under 32 characters because
``vault_alembic_version.version_num`` is ``varchar(32)`` -- a longer one fails at
the very end of the upgrade, after the DDL has run.

Revision ID: 0015_note_compile_declined
Revises: 0014_oauth_refresh_and_csrf
Create Date: 2026-08-24
"""

from alembic import op


revision = "0015_note_compile_declined"
down_revision = "0014_oauth_refresh_and_csrf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            ADD COLUMN compile_declined_at timestamptz;

        ALTER TABLE vault.vault_documents
            ADD CONSTRAINT vault_documents_decline_is_note_only
            CHECK (kind = 'note' OR compile_declined_at IS NULL);
        """
    )


def downgrade() -> None:
    """Refuse rather than silently discard a librarian's judgement.

    A non-null ``compile_declined_at`` records that a compiler was shown a note
    and decided it did not warrant a page. The old schema cannot hold that, and
    the old schema's substitute -- the frontier -- cannot be reconstructed from
    it: a frontier is one timestamp for a whole run, and these are per note.

    Dropping the column therefore does not restore the previous behaviour, it
    re-offers every declined note forever, which is the state ADR 0027 was
    written to avoid. Migration 0012 refuses on the same grounds for the same
    kind of fact.

    An operator who really wants the old shape must clear the column first,
    which is a deliberate act rather than a side effect of a rollback.
    """

    op.execute(
        """
        DO $$
        DECLARE
            declined bigint;
        BEGIN
            SELECT count(*) INTO declined
            FROM vault.vault_documents
            WHERE compile_declined_at IS NOT NULL;

            IF declined > 0 THEN
                RAISE EXCEPTION
                    'cannot drop compile_declined_at: % note(s) carry a '
                    'compile judgement, and the frontier this schema plans '
                    'from cannot represent it. Clear the column before '
                    'downgrading.',
                    declined;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE vault.vault_documents
            DROP CONSTRAINT IF EXISTS vault_documents_decline_is_note_only;

        ALTER TABLE vault.vault_documents
            DROP COLUMN IF EXISTS compile_declined_at;
        """
    )
