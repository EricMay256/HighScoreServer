"""
Runs the labelled query set against live search and reports what it found.

Retrieval has had no regression signal. Fusion constants, the lexical arm's
disjunction (vault ADR 0007), the candidate depth, a second embedding profile,
and the shape of a search hit have all been changed on reasoning alone, because
nothing measured whether a change helped. This script is that measurement, and
`app/vault/retrieval_cases.py` is the set it runs.

It reports three things:

    recall@k       Of the documents a case says are relevant, how many appeared
                   in the top k. The headline number.

    MRR            Mean reciprocal rank of the *best* relevant hit. Recall says
                   whether the answer was on the page; MRR says whether the
                   caller would have had to read past three wrong ones to reach
                   it, which is what actually costs tokens now that a hit is a
                   candidate rather than a document.

    per category   Broken out, and this is the point rather than a courtesy.
                   Vault ADR 0034 defers chunking until a labelled set shows
                   that whole-document vectors lose narrow section-level
                   questions. Only the `narrow_section` row can answer that; an
                   aggregate number would hide it inside four categories where
                   document-level retrieval is expected to do well.

**Labels are provisional until reviewed**, so validated and unvalidated cases
are reported separately. A provisional label is a judgement by the same agent
that changed the search response; scoring it alongside a confirmed one and
reporting a single number would launder that.

**Unresolvable labels are reported, never skipped.** Cases name documents by
title because ids churn across import generations. A title that no longer
resolves means the corpus moved, and silently dropping it would quietly shrink
the denominator until the score improved for no reason.

Reads the database. Embeds one short query per case if a provider is
configured, so a full run costs roughly twenty embedding calls -- well under a
cent. Pass --lexical-only to skip the API entirely and measure the
`not_configured` deployment shape instead.

Usage:
    Full run:            python -m scripts.measure_retrieval_quality
    No API calls:        python -m scripts.measure_retrieval_quality --lexical-only
    Only confirmed:      python -m scripts.measure_retrieval_quality --validated-only
    Show every miss:     python -m scripts.measure_retrieval_quality --show-misses

Environment variables:
    DATABASE_URL               Required. Vault schema must already be migrated.
    VAULT_EMBEDDING_API_KEY    Required unless --lexical-only.
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass

from app.env import load_environment
from app.vault.constants import resolve_text_search_config
from app.vault.db import create_vault_engine, describe_database
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.retrieval_cases import RETRIEVAL_CASES, RetrievalCase
from app.vault.service import VaultSearchService, VaultTransactionService
from app.vault.settings import EmbeddingSettings, VaultSettings


# Reported at both depths on purpose. Five is what a caller reads; ten is the
# search default, and the gap between them says whether the answer was found
# but buried -- which is a fusion problem rather than a retrieval one.
RECALL_DEPTHS = (5, 10)

# Deep enough that a miss is a real miss rather than a page-size artifact.
SEARCH_LIMIT = 10


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """What one case found, and whether it was a fair question to ask.

    Labels are written as titles and scored as ``vault_path``, and the split is
    deliberate in both directions.

    A title is what a human can write and read in a case file, and it is what
    the misses report prints. It is not an identity: `vault_path` carries a
    unique constraint and `title` carries none, and the corpus deliberately
    permits two documents to share a title (`test_wikilinks` pins that as
    legitimate, and the resolver refuses to guess between them). Scoring on a
    non-unique key let an unrelated document with a matching title count as a
    relevant hit -- inflating the numbers this harness exists to decide a
    design on, and doing it silently.

    The path is not a *better* label, only an unambiguous one: every document
    lives under `Agent/notes/` or `Agent/wiki/`, and the leaf is the title
    slugified and truncated to a filename. That is why it is the scoring key
    and never the thing displayed.
    """

    case: RetrievalCase
    returned_paths: tuple[str, ...]
    returned_titles: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    # Labels naming no active readable document, and labels naming more than
    # one. Both make the case unscoreable rather than merely narrower.
    unresolvable: tuple[str, ...]
    ambiguous: tuple[str, ...]
    vector_status: str

    @property
    def valid(self) -> bool:
        """Whether this case's labels describe exactly the documents they name."""

        return not self.unresolvable and not self.ambiguous

    def recall_at(self, depth: int) -> float | None:
        """Share of relevant documents inside the top ``depth``.

        None for an invalid case, and an invalid case is excluded from the run
        rather than from the denominator. Dropping a missing label instead --
        which this used to do -- lets a two-document case that has lost one
        document score 1.0 for finding the survivor, so corpus drift reads as
        an improvement in retrieval quality.
        """

        if not self.valid:
            return None
        wanted = set(self.relevant_paths)
        if not wanted:
            return None
        found = wanted.intersection(self.returned_paths[:depth])
        return len(found) / len(wanted)

    @property
    def reciprocal_rank(self) -> float | None:
        """1/rank of the best relevant hit, or 0.0 if none appeared."""

        if not self.valid or not self.relevant_paths:
            return None
        wanted = set(self.relevant_paths)
        for position, path in enumerate(self.returned_paths, start=1):
            if path in wanted:
                return 1.0 / position
        return 0.0


async def run_case(
    service: VaultSearchService,
    case: RetrievalCase,
    corpus: dict[str, tuple[str, ...]],
) -> CaseOutcome:
    outcome = await service.search(case.query, SEARCH_LIMIT)

    resolved: list[str] = []
    unresolvable: list[str] = []
    ambiguous: list[str] = []
    for title in case.relevant_titles:
        paths = corpus.get(title, ())
        if not paths:
            unresolvable.append(title)
        elif len(paths) > 1:
            ambiguous.append(title)
        else:
            resolved.append(paths[0])

    return CaseOutcome(
        case=case,
        returned_paths=tuple(result.document.vault_path for result in outcome.results),
        returned_titles=tuple(result.document.title for result in outcome.results),
        relevant_paths=tuple(resolved),
        unresolvable=tuple(unresolvable),
        ambiguous=tuple(ambiguous),
        vector_status=str(outcome.vector_status),
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _format(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def report(outcomes: list[CaseOutcome], show_misses: bool) -> int:
    """Print the run. Returns the exit code: nonzero means do not quote it.

    An aggregate is only a measurement if every case in it asked the same
    question of the same corpus. Two things break that, and both used to be
    printed as a note above numbers that were published anyway.
    """

    invalid = [outcome for outcome in outcomes if not outcome.valid]
    if invalid:
        print("\nINVALID LABELS -- this run cannot be scored:")
        for outcome in invalid:
            for title in outcome.unresolvable:
                print(f"  [missing]   {title}")
            for title in outcome.ambiguous:
                print(f"  [ambiguous] {title}")
        print(
            "  A label names a document by title because ids churn. A title "
            "that\n  resolves to nothing means the corpus moved; one that "
            "resolves to two\n  documents does not name either of them. "
            "Neither is a narrower case --\n  scoring around them lets drift "
            "read as an improvement."
        )

    if invalid:
        print(
            "\nAggregates suppressed. Every selected case has to resolve "
            "before any\nnumber here means anything."
        )
        _report_provisional(outcomes)
        _report_misses(outcomes, show_misses)
        return 1

    by_status: dict[str, list[CaseOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_status[outcome.vector_status].append(outcome)

    print(f"\nvector_status: {', '.join(sorted(by_status))}")

    if len(by_status) == 1:
        _print_table(outcomes, total_label="ALL")
        _report_provisional(outcomes)
        _report_misses(outcomes, show_misses)
        return 0

    # Mixed. Each mode gets its own table and there is no combined row,
    # because there is no question a combined row answers: `used` is hybrid,
    # `not_configured` is a deployment that is lexical by choice, and `failed`
    # is a hybrid run that degraded. Pooling the last two as "lexical" would
    # be its own error -- one is a baseline, the other is an accident.
    print(
        "  MIXED -- reported per mode, with no combined figure. A transient "
        "provider\n  failure changes what part of a run measured, and an "
        "average across modes\n  is not a baseline for any of them."
    )
    for status in sorted(by_status):
        subset = by_status[status]
        print(f"\n--- {status} ({len(subset)} case(s)) {_MODE_NOTE.get(status, '')}")
        _print_table(subset, total_label=status)

    print(
        "\nExit code is nonzero: the run did not measure one thing. The "
        "per-mode\nnumbers above are usable on their own terms; there is no "
        "total to quote."
    )
    _report_provisional(outcomes)
    _report_misses(outcomes, show_misses)
    return 1


# What a status means for the numbers under it, so a table cannot be read as a
# baseline it is not.
_MODE_NOTE = {
    "used": "-- hybrid",
    "not_configured": "-- lexical baseline, no provider configured",
    "failed": "-- DEGRADED: provider errored, not a lexical baseline",
}


def _print_table(outcomes: list[CaseOutcome], *, total_label: str) -> None:
    by_category: dict[str, list[CaseOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_category[outcome.case.category].append(outcome)

    header = "  ".join(f"r@{depth}" for depth in RECALL_DEPTHS)
    print(f"\n{'category':<18} {'n':>3}  {header}    MRR")
    for category in sorted(by_category):
        subset = by_category[category]
        _print_row(category, subset)
    _print_row(total_label, outcomes)


def _print_row(label: str, subset: list[CaseOutcome]) -> None:
    recalls = [
        _format(_mean([r for r in (o.recall_at(d) for o in subset) if r is not None]))
        for d in RECALL_DEPTHS
    ]
    mrr = _mean([r for r in (o.reciprocal_rank for o in subset) if r is not None])
    print(f"{label:<18} {len(subset):>3}  {'  '.join(recalls)}  {_format(mrr)}")


def _report_misses(outcomes: list[CaseOutcome], show_misses: bool) -> None:
    misses = [o for o in outcomes if o.reciprocal_rank == 0.0]
    if misses:
        print(f"\n{len(misses)} case(s) returned no relevant document at all:")
        for outcome in misses:
            print(f"  [{outcome.case.category}] {outcome.case.query}")
            if show_misses:
                # Titles, not paths. Scoring uses the path because it is the
                # unique key; a human reading a miss wants the label they
                # wrote. The path's leaf is a slug of the title truncated to
                # fit a filename, so printing it here would show a worse
                # version of the same string.
                print(f"      wanted: {', '.join(outcome.case.relevant_titles)}")
                for title in outcome.returned_titles[:5]:
                    print(f"      got   : {title}")
    print()


def _report_provisional(outcomes: list[CaseOutcome]) -> None:
    provisional = sum(1 for o in outcomes if not o.case.validated)
    if provisional:
        print(
            f"\n{provisional} of {len(outcomes)} cases carry PROVISIONAL labels, "
            "authored by the\nsame agent that changed the search response. "
            "Confirm them against the\ncorpus and set `validated=True` before "
            "quoting these numbers as a baseline."
        )


async def run(lexical_only: bool, validated_only: bool, show_misses: bool) -> int:
    settings = VaultSettings.from_environment()
    if not settings.enabled:
        print("VAULT_ENABLED is false; nothing to measure.", file=sys.stderr)
        return 1

    cases = [
        case
        for case in RETRIEVAL_CASES
        if case.validated or not validated_only
    ]
    if not cases:
        print(
            "No validated cases yet. Confirm some labels first, or drop "
            "--validated-only.",
            file=sys.stderr,
        )
        return 1

    engine, observer = create_vault_engine(settings)
    provider = None
    try:
        if not lexical_only:
            provider = create_embedding_provider(EmbeddingSettings.from_environment())

        print(f"database  : {describe_database(settings.database_url)}")
        print(f"cases     : {len(cases)} of {len(RETRIEVAL_CASES)}")

        transactions = VaultTransactionService(engine, observer)
        service = VaultSearchService(
            transactions=transactions,
            provider=provider,
            text_search_config=resolve_text_search_config(),
        )

        # Every title in the corpus and the paths it names, so a label that
        # resolves to nothing and a label that resolves to two documents are
        # both distinguished from a search miss. One query, not one per case.
        corpus = await _corpus_by_title(transactions)

        outcomes = [await run_case(service, case, corpus) for case in cases]
        # The report decides the exit code: an unscoreable run has to be
        # noticeable to whatever ran it, not only to whoever reads the output.
        return report(outcomes, show_misses)
    finally:
        if provider is not None:
            await provider.aclose()
        await engine.dispose()


async def _corpus_by_title(
    transactions: VaultTransactionService,
) -> dict[str, tuple[str, ...]]:
    """Every active readable title, mapped to the paths carrying it.

    A tuple rather than a single path because a shared title is legal here, and
    the harness has to be able to say so: a label naming two documents names
    neither, and guessing which was meant is how a measurement stops measuring.
    """

    from collections import defaultdict as _defaultdict

    from sqlalchemy import select

    from app.vault.domain import DocumentStatus
    from app.vault.read_policy import readable_path_predicate
    from app.vault.tables import vault_documents

    statement = select(vault_documents.c.title, vault_documents.c.vault_path).where(
        vault_documents.c.status == DocumentStatus.ACTIVE.value,
        readable_path_predicate(),
    )
    index: dict[str, list[str]] = _defaultdict(list)
    async with transactions.transaction() as connection:
        for title, path in (await connection.execute(statement)).all():
            index[title].append(path)
    return {title: tuple(sorted(paths)) for title, paths in index.items()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the labelled query set against live search and report "
            "recall@k and MRR, broken out by case category."
        ),
    )
    parser.add_argument(
        "--lexical-only",
        action="store_true",
        help=(
            "Skip the embedding provider entirely. Measures the "
            "`not_configured` deployment and costs nothing."
        ),
    )
    parser.add_argument(
        "--validated-only",
        action="store_true",
        help="Score only cases whose labels a human has confirmed.",
    )
    parser.add_argument(
        "--show-misses",
        action="store_true",
        help="For each total miss, print what was wanted and what came back.",
    )
    arguments = parser.parse_args()

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py, conftest.py, and the sibling measure_ scripts. No-op on
    # Linux/Heroku.
    coroutine = run(
        arguments.lexical_only, arguments.validated_only, arguments.show_misses
    )
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
