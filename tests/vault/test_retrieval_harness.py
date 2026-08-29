"""What the retrieval harness will and will not publish a number from.

The measurements here decide a design question -- document-level against
chunked retrieval -- so the failure that matters is not a wrong number but a
number that looks quotable and is not. Every test below is about the harness
refusing to score rather than about how it scores.

No database: `report` and `run_case` are pure over their inputs, and the
corpus index they take is exactly the seam a test can supply.
"""

import pytest

from app.vault.retrieval_cases import RetrievalCase
from scripts.measure_retrieval_quality import CaseOutcome, report


def _case(*titles: str) -> RetrievalCase:
    return RetrievalCase(
        query="how does the dedup gate decide",
        category="narrow_section",
        relevant_titles=titles,
        rationale="Fixture.",
        validated=True,
    )


def _outcome(
    *,
    relevant_paths: tuple[str, ...] = ("a.md",),
    returned_paths: tuple[str, ...] = ("a.md",),
    unresolvable: tuple[str, ...] = (),
    ambiguous: tuple[str, ...] = (),
    vector_status: str = "used",
    titles: tuple[str, ...] = ("A",),
) -> CaseOutcome:
    return CaseOutcome(
        case=_case(*titles),
        returned_paths=returned_paths,
        returned_titles=titles,
        relevant_paths=relevant_paths,
        unresolvable=unresolvable,
        ambiguous=ambiguous,
        vector_status=vector_status,
    )


def test_one_missing_label_invalidates_the_case_rather_than_the_denominator() -> None:
    """A two-document case that has lost one document must not score 1.0.

    Dropping the missing label from the denominator made corpus drift read as
    an improvement: find the survivor, score perfectly, and the number that is
    supposed to detect the drift reports the opposite.
    """

    outcome = _outcome(
        relevant_paths=("a.md",),
        returned_paths=("a.md",),
        unresolvable=("A Note That Was Deleted",),
    )

    assert not outcome.valid
    assert outcome.recall_at(5) is None
    assert outcome.reciprocal_rank is None


def test_a_case_whose_labels_all_vanished_is_invalid_not_merely_unscored() -> None:
    outcome = _outcome(relevant_paths=(), unresolvable=("Gone", "Also Gone"))

    assert not outcome.valid
    assert outcome.recall_at(10) is None


def test_a_title_naming_two_documents_is_invalid() -> None:
    """Titles are not unique in this corpus and the tests elsewhere say so.

    Scoring by title let an unrelated document with a matching title count as
    a relevant hit. A label that resolves to two documents names neither, so
    the case cannot be scored and must not be guessed at.
    """

    outcome = _outcome(relevant_paths=(), ambiguous=("Shared Title",))

    assert not outcome.valid
    assert outcome.recall_at(5) is None
    assert outcome.reciprocal_rank is None


def test_an_invalid_run_suppresses_its_aggregates_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The number must not be quotable, and the exit code has to say so to
    whatever ran the script rather than only to whoever reads it."""

    exit_code = report(
        [_outcome(), _outcome(unresolvable=("Gone",))], show_misses=False
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "INVALID LABELS" in output
    assert "Aggregates suppressed" in output
    # The table itself never gets printed, so there is nothing to misread.
    assert "MRR" not in output


def test_a_mixed_status_run_is_neither_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transient provider failure changes what half a run measured.

    Averaging a hybrid case with one that fell back to lexical produces a
    number that is not a hybrid baseline and not a lexical one.
    """

    exit_code = report(
        [_outcome(vector_status="used"), _outcome(vector_status="failed")],
        show_misses=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "MIXED" in output
    assert "MRR" not in output


@pytest.mark.parametrize("status", ["used", "not_configured"])
def test_a_homogeneous_valid_run_publishes_its_aggregates(
    status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both single-mode runs are legitimate: hybrid, and the `--lexical-only`
    baseline that measures a deployment with no provider configured."""

    exit_code = report(
        [
            _outcome(vector_status=status),
            _outcome(
                vector_status=status,
                relevant_paths=("b.md",),
                returned_paths=("x.md", "b.md"),
            ),
        ],
        show_misses=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "MRR" in output
    assert "ALL" in output


def test_scoring_uses_paths_so_a_matching_title_is_not_a_hit() -> None:
    """The inflation this replaced: an unrelated document sharing a title used
    to count as the relevant one."""

    outcome = _outcome(
        relevant_paths=("Agent/notes/the-one-meant.md",),
        returned_paths=("Agent/notes/a-different-note.md",),
        titles=("Shared Title",),
    )

    assert outcome.valid
    assert outcome.recall_at(5) == 0.0
    assert outcome.reciprocal_rank == 0.0
