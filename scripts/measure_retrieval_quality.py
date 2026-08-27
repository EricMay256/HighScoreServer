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
    """What one case found."""

    case: RetrievalCase
    returned_titles: tuple[str, ...]
    unresolvable: tuple[str, ...]
    vector_status: str

    @property
    def resolvable_titles(self) -> tuple[str, ...]:
        return tuple(
            title
            for title in self.case.relevant_titles
            if title not in self.unresolvable
        )

    def recall_at(self, depth: int) -> float | None:
        """Share of relevant documents inside the top ``depth``.

        None when nothing in the case resolves against this corpus -- which is
        distinct from a recall of zero, and must stay distinct: one means the
        search missed, the other means the question was never asked.
        """

        wanted = set(self.resolvable_titles)
        if not wanted:
            return None
        found = wanted.intersection(self.returned_titles[:depth])
        return len(found) / len(wanted)

    @property
    def reciprocal_rank(self) -> float | None:
        """1/rank of the best relevant hit, or 0.0 if none appeared."""

        if not self.resolvable_titles:
            return None
        wanted = set(self.resolvable_titles)
        for position, title in enumerate(self.returned_titles, start=1):
            if title in wanted:
                return 1.0 / position
        return 0.0


async def run_case(service: VaultSearchService, case: RetrievalCase,
                   known_titles: set[str]) -> CaseOutcome:
    outcome = await service.search(case.query, SEARCH_LIMIT)
    return CaseOutcome(
        case=case,
        returned_titles=tuple(result.document.title for result in outcome.results),
        unresolvable=tuple(
            title for title in case.relevant_titles if title not in known_titles
        ),
        vector_status=str(outcome.vector_status),
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _format(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def report(outcomes: list[CaseOutcome], show_misses: bool) -> None:
    unresolvable = {
        title for outcome in outcomes for title in outcome.unresolvable
    }
    if unresolvable:
        print("\nUNRESOLVABLE LABELS -- these cases scored nothing:")
        for title in sorted(unresolvable):
            print(f"  {title}")
        print(
            "  A label names a document by title because ids churn. A title "
            "that\n  no longer resolves means the corpus moved, not that the "
            "case passed."
        )

    statuses = {outcome.vector_status for outcome in outcomes}
    print(f"\nvector_status: {', '.join(sorted(statuses))}")

    by_category: dict[str, list[CaseOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_category[outcome.case.category].append(outcome)

    header = "  ".join(f"r@{depth}" for depth in RECALL_DEPTHS)
    print(f"\n{'category':<18} {'n':>3}  {header}    MRR")
    for category in sorted(by_category):
        subset = by_category[category]
        recalls = [
            _format(_mean([r for r in (o.recall_at(d) for o in subset) if r is not None]))
            for d in RECALL_DEPTHS
        ]
        mrr = _mean(
            [r for r in (o.reciprocal_rank for o in subset) if r is not None]
        )
        print(
            f"{category:<18} {len(subset):>3}  {'  '.join(recalls)}  {_format(mrr)}"
        )

    overall_recall = [
        _format(_mean([r for r in (o.recall_at(d) for o in outcomes) if r is not None]))
        for d in RECALL_DEPTHS
    ]
    overall_mrr = _mean(
        [r for r in (o.reciprocal_rank for o in outcomes) if r is not None]
    )
    print(
        f"{'ALL':<18} {len(outcomes):>3}  {'  '.join(overall_recall)}  "
        f"{_format(overall_mrr)}"
    )

    provisional = sum(1 for o in outcomes if not o.case.validated)
    if provisional:
        print(
            f"\n{provisional} of {len(outcomes)} cases carry PROVISIONAL labels, "
            "authored by the\nsame agent that changed the search response. "
            "Confirm them against the\ncorpus and set `validated=True` before "
            "quoting these numbers as a baseline."
        )

    misses = [o for o in outcomes if o.reciprocal_rank == 0.0]
    if misses:
        print(f"\n{len(misses)} case(s) returned no relevant document at all:")
        for outcome in misses:
            print(f"  [{outcome.case.category}] {outcome.case.query}")
            if show_misses:
                print(f"      wanted: {', '.join(outcome.resolvable_titles)}")
                for title in outcome.returned_titles[:5]:
                    print(f"      got   : {title}")
    print()


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

        # Every title in the corpus, so an unresolvable label is distinguished
        # from a search miss. One query rather than one per case.
        known = await _known_titles(transactions)

        outcomes = [await run_case(service, case, known) for case in cases]
        report(outcomes, show_misses)
    finally:
        if provider is not None:
            await provider.aclose()
        await engine.dispose()
    return 0


async def _known_titles(transactions: VaultTransactionService) -> set[str]:
    from sqlalchemy import select

    from app.vault.domain import DocumentStatus
    from app.vault.read_policy import readable_path_predicate
    from app.vault.tables import vault_documents

    statement = select(vault_documents.c.title).where(
        vault_documents.c.status == DocumentStatus.ACTIVE.value,
        readable_path_predicate(),
    )
    async with transactions.transaction() as connection:
        rows = (await connection.execute(statement)).scalars().all()
    return set(rows)


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
