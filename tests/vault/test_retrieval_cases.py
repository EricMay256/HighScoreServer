"""The labelled query set's internal consistency.

Whether a label is *correct* is a judgement about the corpus, and no test can
settle it -- that is what `RetrievalCase.validated` and a human reviewer are
for. What a test can settle is whether the set is coherent enough to measure
anything: no duplicate queries silently double-weighting one result, no case
with nothing to find, and each category actually populated with the shape it
claims to probe.

These run without a database. Resolving labels against a real corpus is
`scripts/measure_retrieval_quality.py`'s job, because it depends on which
corpus the caller points at -- a test asserting that a title exists would pass
locally and fail in CI, where the corpus is empty by design.
"""

from collections import Counter

from app.vault.retrieval_cases import RETRIEVAL_CASES, RetrievalCase


def test_every_case_has_something_to_find() -> None:
    empty = [case.query for case in RETRIEVAL_CASES if not case.relevant_titles]

    assert empty == [], f"cases with no relevant documents: {empty}"


def test_queries_are_unique() -> None:
    """A repeated query weights one result twice without saying so."""

    counts = Counter(case.query for case in RETRIEVAL_CASES)
    repeated = [query for query, count in counts.items() if count > 1]

    assert repeated == [], f"duplicate queries: {repeated}"


def test_relevant_titles_are_unique_within_a_case() -> None:
    """Recall is set membership; a repeat would inflate the denominator."""

    offenders = [
        case.query
        for case in RETRIEVAL_CASES
        if len(set(case.relevant_titles)) != len(case.relevant_titles)
    ]

    assert offenders == [], f"cases with duplicate titles: {offenders}"


def test_every_case_carries_a_checkable_rationale() -> None:
    """The rationale is what a reviewer validates against, so it must exist.

    A label with no stated reason cannot be confirmed or refuted, which makes
    it a number the evaluation would report without anyone able to defend it.
    """

    thin = [
        case.query
        for case in RETRIEVAL_CASES
        if len(case.rationale.strip()) < 40
    ]

    assert thin == [], f"cases whose rationale is too thin to check: {thin}"


def test_every_category_is_populated() -> None:
    """An empty category cannot answer the question it was added to ask."""

    counts = Counter(case.category for case in RETRIEVAL_CASES)
    expected = {
        "narrow_section",
        "broad_document",
        "exact_term",
        "paraphrase",
        "atomic_note",
    }

    assert set(counts) == expected
    assert all(count >= 3 for count in counts.values()), (
        f"a category with fewer than three cases reports noise: {dict(counts)}"
    )


def test_narrow_section_cases_all_name_a_multi_section_document() -> None:
    """The category's premise, asserted rather than assumed.

    A `narrow_section` case exists to ask whether a whole-document vector
    loses a question answered inside one section of a long document. If its
    relevant set names only short atomic notes, it is not probing that at all
    -- it is a paraphrase case wearing the wrong label, and it would report on
    ADR 0034's fourth trigger without testing it.

    Wiki pages are the multi-section documents in this corpus: measured
    2026-08-26, the median page carries four headings and the median note
    carries none.
    """

    def names_a_page(case: RetrievalCase) -> bool:
        # Title-cased, space-separated titles are the compiled pages; notes are
        # declarative sentences. Crude, and adequate -- the alternative is a
        # database round trip inside a unit test.
        return any(
            title == title.strip() and not title.endswith(".") and title[:1].isupper()
            and len(title.split()) <= 8
            for title in case.relevant_titles
        )

    offenders = [
        case.query
        for case in RETRIEVAL_CASES
        if case.category == "narrow_section" and not names_a_page(case)
    ]

    assert offenders == [], (
        "narrow_section cases must name a multi-section document, or they "
        f"cannot probe what the category exists for: {offenders}"
    )


def test_labels_are_provisional_until_reviewed() -> None:
    """Records the review state rather than enforcing an outcome.

    This asserts only that the flag is a real boolean on every case. The set
    ships provisional on purpose: these labels were authored by the same agent
    that changed the search response, and the flag is how a reviewer's pass is
    recorded. Flipping cases to True as they are confirmed is the intended
    edit, and this test keeps passing when they are.
    """

    assert all(isinstance(case.validated, bool) for case in RETRIEVAL_CASES)
