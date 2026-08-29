"""
Reports how much of the corpus would be eligible for retrieval chunking.

The vault embeds one vector per document (vault ADR 0034). Splitting long
documents into separately-addressable chunks is the standard next move, and the
standard mistake is to adopt it before checking whether the corpus contains
documents long enough to need it. This script answers that from the corpus
rather than from intuition.

It reports three things:

    length          Body length per document, in characters and estimated
                    tokens, split by kind. Notes and wiki pages are different
                    populations -- a page is a synthesis of several notes -- and
                    a threshold chosen against their union fits neither.

    structure       Heading count and section-size distribution. A document is
                    worth chunking when it has multiple substantial sections
                    covering distinct ideas; one long undivided argument is not
                    improved by cutting it at an arbitrary offset.

    eligibility     How many documents each candidate threshold would select,
                    and how many chunks that would produce. Chunk count is the
                    multiplier on every later storage and index estimate, so it
                    is the number that decides whether chunking is cheap.

**Token counts are estimated, not tokenized.** A real count needs the model's
tokenizer, and adding one as a dependency to answer a sizing question would be
the tail wagging the dog. English prose runs about four characters per token,
and the thresholds this feeds are themselves round numbers -- treat a reported
token count as accurate to within about ten percent, and do not quote it as a
billing figure.

Reads the database and writes nothing to it. Makes no API calls and costs
nothing to run.

Usage:
    Whole corpus:       python -m scripts.measure_chunk_eligibility
    Notes only:         python -m scripts.measure_chunk_eligibility --kind note
    Show the longest:   python -m scripts.measure_chunk_eligibility --show 15

Environment variables:
    DATABASE_URL    Required. Vault schema must already be migrated.
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.env import load_environment
from app.vault.db import create_vault_engine, describe_database
from app.vault.domain import DocumentStatus
from app.vault.measurement import percentile
from app.vault.read_policy import readable_path_predicate
from app.vault.settings import VaultSettings
from app.vault.snippet import fenced_line_mask
from app.vault.tables import vault_documents


# Characters per token for English prose. See the module docstring: this is an
# estimate standing in for a tokenizer, chosen so the script needs no
# dependency it would otherwise not have.
CHARS_PER_TOKEN = 4.0

# The candidate ceilings from the efficiency assessment's chunk-eligibility
# policy, in estimated tokens. Deliberately its numbers rather than ones
# derived here: the point of the run is to find out what this corpus does at
# thresholds someone else proposed, not to fit thresholds to the corpus and
# then be reassured by them.
CANDIDATE_THRESHOLDS = (800, 1200, 2000)

# An ATX heading. Setext headings are not matched: the corpus does not use
# them, and a false positive here would overstate how sectioned a document is.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

@dataclass(frozen=True, slots=True)
class DocumentShape:
    """One document's size and structure, as this question sees it."""

    document_id: str
    kind: str
    title: str
    characters: int
    headings: int
    section_characters: tuple[int, ...]

    @property
    def tokens(self) -> int:
        return round(self.characters / CHARS_PER_TOKEN)

    @property
    def substantial_sections(self) -> int:
        """Sections big enough to be worth addressing separately.

        400 characters is about a paragraph. A document whose headings carve
        it into one-line stubs has structure but nothing to retrieve, and
        counting those would make it look chunkable when it is not.
        """

        return sum(1 for size in self.section_characters if size >= 400)


def strip_fenced_blocks(body: str) -> str:
    """Blank out fenced regions, preserving offsets.

    Replacing with spaces rather than deleting keeps every later character
    index meaningful, so section sizes measured against the result still
    describe the real document.

    The fence scanning is `snippet.fenced_line_mask` rather than a second copy
    here. The copy this replaces paired fence lines by *position*, ignoring the
    marker character and length: inside a four-backtick block a three-backtick
    content line was read as the close, the real close became a new unclosed
    opener, and from there headings inside code were counted while genuine
    ones after the block were masked. Those counts are what ADR 0034's chunking
    decision rests on, so the bug was in the measurement rather than in a
    preview.
    """

    return "\n".join(
        " " * len(line) if fenced else line
        for line, fenced in zip(
            body.split("\n"), fenced_line_mask(body), strict=True
        )
    )


def shape_of(document_id: str, kind: str, title: str, body: str) -> DocumentShape:
    """Measure one document's length and section structure."""

    masked = strip_fenced_blocks(body)
    starts = [match.start() for match in _HEADING.finditer(masked)]

    if starts:
        bounds = starts + [len(body)]
        sections = tuple(
            bounds[index + 1] - bounds[index] for index in range(len(starts))
        )
        # Text before the first heading is a section too -- in this corpus it
        # is usually the thesis paragraph, which is the most retrievable part
        # of the document.
        if starts[0] > 0:
            sections = (starts[0],) + sections
    else:
        sections = (len(body),) if body else ()

    return DocumentShape(
        document_id=document_id,
        kind=kind,
        title=title,
        characters=len(body),
        headings=len(starts),
        section_characters=sections,
    )


async def load_shapes(engine: AsyncEngine, kind: str | None) -> list[DocumentShape]:
    """Every active, readable document's body and structure.

    Filtered the way search filters, so the population measured is the one a
    chunked retrieval arm would actually serve. Measuring archived or withheld
    documents would size a corpus nobody queries.
    """

    statement = select(
        vault_documents.c.id,
        vault_documents.c.kind,
        vault_documents.c.title,
        vault_documents.c.body,
    ).where(
        vault_documents.c.status == DocumentStatus.ACTIVE.value,
        readable_path_predicate(),
    )
    if kind is not None:
        statement = statement.where(vault_documents.c.kind == kind)

    async with engine.connect() as connection:
        rows = (await connection.execute(statement)).mappings().all()

    return [
        shape_of(row["id"], row["kind"], row["title"], row["body"] or "")
        for row in rows
    ]


def _distribution(label: str, values: list[int]) -> str:
    if not values:
        return f"  {label:<12} (none)"
    ordered = sorted(values)
    return (
        f"  {label:<12} n={len(ordered):<4} "
        f"min={ordered[0]:<6} median={percentile(ordered, 0.5):<6.0f} "
        f"p75={percentile(ordered, 0.75):<6.0f} p90={percentile(ordered, 0.9):<6.0f} "
        f"max={ordered[-1]}"
    )


def report(shapes: list[DocumentShape], show: int) -> None:
    if not shapes:
        print("No active readable documents. Nothing to measure.")
        return

    kinds = sorted({shape.kind for shape in shapes})

    print(f"\nCorpus: {len(shapes)} active readable documents\n")

    print("Body length, characters")
    for kind in kinds:
        subset = [s.characters for s in shapes if s.kind == kind]
        print(_distribution(kind, subset))
    print(_distribution("all", [s.characters for s in shapes]))

    print("\nBody length, estimated tokens (chars / 4)")
    for kind in kinds:
        subset = [s.tokens for s in shapes if s.kind == kind]
        print(_distribution(kind, subset))
    print(_distribution("all", [s.tokens for s in shapes]))

    print("\nHeadings per document")
    for kind in kinds:
        subset = [s.headings for s in shapes if s.kind == kind]
        print(_distribution(kind, subset))

    print("\nSubstantial sections per document (>= 400 chars)")
    for kind in kinds:
        subset = [s.substantial_sections for s in shapes if s.kind == kind]
        print(_distribution(kind, subset))

    print("\nEligibility at each candidate threshold")
    print(
        "  A document is eligible when it exceeds the threshold AND has at "
        "least two\n  substantial sections -- length alone is one long "
        "argument, which chunking\n  does not improve.\n"
    )
    total_documents = len(shapes)
    for threshold in CANDIDATE_THRESHOLDS:
        long_enough = [s for s in shapes if s.tokens > threshold]
        eligible = [s for s in long_enough if s.substantial_sections >= 2]
        chunks = sum(s.substantial_sections for s in eligible)
        # Every document keeps its whole-document vector (ADR 0034), so the
        # chunk vectors are additional rather than a replacement.
        vectors = total_documents + chunks
        share = len(eligible) / total_documents
        print(
            f"  > {threshold:>4} tokens: {len(long_enough):>3} long, "
            f"{len(eligible):>3} eligible ({share:>4.0%}), "
            f"+{chunks:>3} chunk vectors -> {vectors} total "
            f"({vectors / total_documents:.2f}x today)"
        )

    print(f"\nLongest {show} documents")
    for shape in sorted(shapes, key=lambda s: -s.characters)[:show]:
        print(
            f"  {shape.tokens:>5}t  {shape.headings:>2}h  "
            f"{shape.substantial_sections:>2}s  [{shape.kind}] "
            f"{shape.title[:60]}"
        )
    print()


async def run(kind: str | None, show: int) -> int:
    settings = VaultSettings.from_environment()
    if not settings.enabled:
        print("VAULT_ENABLED is false; nothing to measure.", file=sys.stderr)
        return 1

    engine, _observer = create_vault_engine(settings)
    try:
        print(f"database  : {describe_database(settings.database_url)}")
        shapes = await load_shapes(engine, kind)
        report(shapes, show)
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report how much of the vault corpus would be eligible for "
            "retrieval chunking, and how many vectors that would add."
        ),
    )
    parser.add_argument(
        "--kind",
        choices=("note", "wiki"),
        default=None,
        help="Restrict to one document kind. Default: both, reported separately.",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        metavar="N",
        help="How many of the longest documents to list. Default: 10.",
    )
    arguments = parser.parse_args()

    if arguments.show < 1:
        parser.error("--show must be at least 1")

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py, conftest.py, and the sibling measure_ scripts. No-op on
    # Linux/Heroku.
    coroutine = run(arguments.kind, arguments.show)
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
