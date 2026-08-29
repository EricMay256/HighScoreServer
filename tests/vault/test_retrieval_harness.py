"""What the retrieval harness will and will not publish a number from.

The measurements here decide a design question -- document-level against
chunked retrieval -- so the failure that matters is not a wrong number but a
number that looks quotable and is not. Every test below is about the harness
refusing to score rather than about how it scores.

No database: `report` and `run_case` are pure over their inputs, and the
corpus index they take is exactly the seam a test can supply.
"""

import asyncio

import pytest

from app.vault.retrieval_cases import RetrievalCase
from scripts.measure_retrieval_quality import (
    CaseOutcome,
    _warn_on_embedding_gaps,
    report,
)


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
    assert exit_code == 1, (
        "An unscoreable run exited zero. Do not fix this by relaxing the exit "
        "code: it is how a caller that does not read the output learns the "
        "numbers are not quotable, and this harness exists to decide a design "
        "question. Either label validation stopped invalidating the run, or "
        "the aggregate path no longer checks it."
    )
    assert "INVALID LABELS" in output
    assert "Aggregates suppressed" in output
    # The table itself never gets printed, so there is nothing to misread.
    assert "MRR" not in output


def test_a_mixed_status_run_reports_each_mode_and_no_total(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transient provider failure changes what half a run measured.

    Averaging a hybrid case with one that degraded to lexical produces a number
    that is neither baseline, so there is no combined row -- but the per-mode
    numbers are real and are worth keeping rather than discarding a whole run
    over two failures.
    """

    exit_code = report(
        [
            _outcome(vector_status="used"),
            _outcome(vector_status="failed", relevant_paths=("b.md",),
                     returned_paths=("x.md", "b.md")),
        ],
        show_misses=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "MIXED" in output
    # Each mode gets its own table, labelled for what it is.
    assert "--- used" in output
    assert "--- failed" in output
    assert "DEGRADED" in output
    # And no combined figure, because none of them would answer anything.
    assert "ALL" not in output


def test_a_degraded_mode_is_not_labelled_a_lexical_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`not_configured` is a deployment that is lexical by choice; `failed` is a
    hybrid run that broke. Pooling them as "lexical" would be its own error."""

    report(
        [
            _outcome(vector_status="not_configured"),
            _outcome(vector_status="failed"),
        ],
        show_misses=False,
    )

    output = capsys.readouterr().out
    assert "lexical baseline" in output
    assert "DEGRADED" in output


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


def test_a_miss_is_reported_by_title_not_by_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scoring key and display label are different jobs.

    Every path is `Agent/notes/<slug>.md` or `Agent/wiki/<slug>.md`, and the
    slug is the title truncated to fit a filename -- so showing the path to a
    human reading a miss shows a worse version of the label they wrote.
    """

    report(
        [
            _outcome(
                relevant_paths=("Agent/notes/two-implementations-agreeing-is-str.md",),
                returned_paths=("Agent/notes/something-else.md",),
                titles=("Two implementations agreeing is stronger evidence",),
            )
        ],
        show_misses=True,
    )

    output = capsys.readouterr().out
    assert "wanted: Two implementations agreeing is stronger evidence" in output
    assert "Agent/notes/" not in output


class _StubResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _StubConnection:
    def __init__(self, missing: int) -> None:
        self._missing = missing

    async def execute(self, _statement) -> _StubResult:
        return _StubResult(self._missing)


class _StubTransactions:
    """Just enough of `VaultTransactionService` for the coverage preflight.

    The preflight is one COUNT and a print, so the database is the only thing
    standing between it and a unit test. Stubbing the connection keeps the
    assertion on what the operator is told rather than on SQLAlchemy.
    """

    def __init__(self, missing: int) -> None:
        self._missing = missing

    def transaction(self):
        missing = self._missing

        class _Ctx:
            async def __aenter__(self):
                return _StubConnection(missing)

            async def __aexit__(self, *exc) -> bool:
                return False

        return _Ctx()


def test_the_coverage_preflight_warns_and_does_not_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partial coverage still measures something, so it is said and not refused.

    A document with no row for the active profile is invisible to the vector
    arm while the lexical arm still returns it, so nothing in the response
    looks wrong and recall reads low for a reason no field explains. The
    operator seeing this is most likely mid-migration, which is not an error.
    """

    async def exercise() -> None:
        transactions = _StubTransactions(missing=3)

        await _warn_on_embedding_gaps(transactions, "openai/text-embedding-3-small:1536")

    asyncio.run(exercise())

    output = capsys.readouterr().out
    assert "3 active document(s) have no embedding" in output
    assert "openai/text-embedding-3-small:1536" in output
    assert "floor" in output


def test_full_coverage_says_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ordinary case is silent. A preflight that always prints is one
    nobody reads."""

    asyncio.run(_warn_on_embedding_gaps(_StubTransactions(missing=0), "profile"))

    assert capsys.readouterr().out == ""
