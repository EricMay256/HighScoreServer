"""
Derives ``Policy.flag_at`` for the configured embedding model by measurement.

``flag_at`` is a cosine similarity on a specific model, so it does not transfer
between models. This script produces both sides of the derivation described in
``app/vault/calibration.py``:

    negative side   Pairwise similarity of every active, readable document
                    already in the corpus. Those documents were all judged
                    distinct enough to coexist, so the largest score among them
                    is a floor: a flag_at at or below it flags legitimate work.

    positive side   The reference duplicate pairs in ``calibration.py``, embedded
                    fresh. Each states one insight twice in different words, so
                    the smallest score among them is a ceiling: a flag_at above
                    it misses a real duplicate.

Both sides go through ``assemble_embedding_text``. That is not incidental: the
stored corpus vectors were built from title + aliases + tags + summary + body,
and tags alone move the maximum corpus pair by roughly 0.05. Embedding the
reference pairs as bare prose -- as the first version of this script did --
compares a tag-inflated floor against a tag-free ceiling and understates the
margin, biasing the derivation toward "not separable".

A usable threshold sits strictly between the two, and the script prints one. If
the bands overlap it says so and recommends nothing — leaving ``flag_at`` at 1.0
is the correct outcome, not a failure to produce a number.

Read the corpus half as a *lower bound on safety only*. A corpus assembled under
string dedup is self-selected to contain no duplicates, so it constrains the
false-positive side well and says nothing about the true-positive side. That
asymmetry is exactly why the reference pairs exist.

Record every run in the model register in
``app/vault/docs/embedding-calibration.md``. That file, not this script's
scrollback, is the durable artifact.

This calls a paid API — one request per reference pair, well under a cent. It
reads the database and writes nothing to it.

Usage:
    Full derivation:        python -m scripts.measure_dedup_similarity
    Skip the API call:      python -m scripts.measure_dedup_similarity --corpus-only
    Show more near pairs:   python -m scripts.measure_dedup_similarity --show 20

Environment variables:
    DATABASE_URL               Required. Vault schema must already be migrated.
    VAULT_EMBEDDING_API_KEY    Required unless --corpus-only.
    VAULT_EMBEDDING_PROFILE_ID Optional. Which profile's vectors to read.
"""

import argparse
import asyncio
import math
import statistics
import sys

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.env import load_environment
from app.vault.calibration import REFERENCE_DUPLICATE_PAIRS, derive_flag_at
from app.vault.db import create_vault_engine
from app.vault.domain import DocumentStatus
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embedding_text import assemble_embedding_text
from app.vault.embeddings import EmbeddingError, EmbeddingInputKind
from app.vault.read_policy import readable_path_predicate
from app.vault.settings import EmbeddingSettings, VaultSettings
from app.vault.tables import vault_document_embeddings, vault_documents


def describe_database(url: str) -> str:
    """Host, port, and database name only — never the credential."""

    parsed = make_url(url)
    return f"{parsed.host}:{parsed.port}/{parsed.database}"


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity in pure Python.

    Deliberately not assuming unit vectors. OpenAI returns normalized
    embeddings, so the norms are 1.0 and this is a dot product — but that is a
    property of one provider, and this script exists to evaluate others.
    """

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile — an observed value, never an interpolated one."""

    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


async def load_corpus(engine, profile_id: str) -> list[tuple[str, list[float]]]:
    """Every active, readable document's title and vector under one profile.

    Filtered by ``readable_path_predicate`` and ``status = active`` so the
    sample matches what ``find_similar`` actually compares a contribution
    against. Measuring a population the gate never sees would calibrate the
    wrong distribution.
    """

    statement = (
        select(vault_documents.c.title, vault_document_embeddings.c.embedding)
        .select_from(
            vault_document_embeddings.join(
                vault_documents,
                vault_documents.c.id == vault_document_embeddings.c.document_id,
            )
        )
        .where(
            vault_document_embeddings.c.profile_id == profile_id,
            vault_documents.c.status == DocumentStatus.ACTIVE.value,
            readable_path_predicate(),
        )
        .order_by(vault_documents.c.id)
    )
    async with engine.connect() as connection:
        result = await connection.execute(statement)
        return [(row["title"], list(row["embedding"])) for row in result.mappings()]


def report_distribution(scores: list[float]) -> None:
    print("\nPairwise similarity, existing corpus")
    print("-" * 36)
    print(f"  pairs     : {len(scores)}")
    print(f"  min       : {min(scores):6.4f}")
    print(f"  p50       : {percentile(scores, 0.50):6.4f}")
    print(f"  p90       : {percentile(scores, 0.90):6.4f}")
    print(f"  p95       : {percentile(scores, 0.95):6.4f}")
    print(f"  p99       : {percentile(scores, 0.99):6.4f}")
    print(f"  max       : {max(scores):6.4f}")
    print(f"  mean      : {statistics.fmean(scores):6.4f}")


def report_closest(
    pairs: list[tuple[float, str, str]],
    show: int,
) -> None:
    """The near misses, which are what a reviewer actually has to judge."""

    print(f"\nClosest {min(show, len(pairs))} pairs — these must NOT flag")
    print("-" * 60)
    for score, left, right in sorted(pairs, reverse=True)[:show]:
        print(f"  {score:.4f}  {left[:34]:<34}  ||  {right[:34]}")


async def measure_reference_pairs(settings: EmbeddingSettings) -> list[float] | None:
    """Embed each known-duplicate pair and score it.

    Each side is rendered by ``assemble_embedding_text`` -- the same function
    that produced every stored corpus vector -- so the ceiling and the floor
    are measured on the same text shape. See this module's docstring.

    Returns None when the provider is unusable, which is distinct from an empty
    list: no measurement at all must not be read as "no duplicates separate".
    """

    provider = create_embedding_provider(settings)
    scores: list[float] = []
    try:
        for index, (left, right) in enumerate(REFERENCE_DUPLICATE_PAIRS, start=1):
            try:
                vectors = await provider.embed(
                    [
                        assemble_embedding_text(left),
                        assemble_embedding_text(right),
                    ],
                    EmbeddingInputKind.DOCUMENT,
                )
            except EmbeddingError as exc:
                # Type only, never the message: it can carry request content.
                print(f"  pair {index}: {type(exc).__name__}", file=sys.stderr)
                return None
            scores.append(cosine(list(vectors[0]), list(vectors[1])))
    finally:
        await provider.aclose()
    return scores


async def run(corpus_only: bool, show: int) -> int:
    vault_settings = VaultSettings.from_environment()
    embedding_settings = EmbeddingSettings.from_environment()

    print(f"database  : {describe_database(vault_settings.database_url)}")
    print(f"profile   : {embedding_settings.profile_id}")
    print(f"model     : {embedding_settings.model}")

    engine, _observer = create_vault_engine(vault_settings)
    try:
        corpus = await load_corpus(engine, embedding_settings.profile_id)
    finally:
        await engine.dispose()

    if len(corpus) < 2:
        print(
            f"\nOnly {len(corpus)} embedded document(s) under this profile — "
            "not enough to measure a pairwise distribution.",
            file=sys.stderr,
        )
        return 1

    print(f"documents : {len(corpus)}")

    pairs: list[tuple[float, str, str]] = []
    for index, (left_title, left_vector) in enumerate(corpus):
        for right_title, right_vector in corpus[index + 1 :]:
            pairs.append(
                (cosine(left_vector, right_vector), left_title, right_title)
            )

    distinct_scores = [score for score, _, _ in pairs]
    report_distribution(distinct_scores)
    report_closest(pairs, show)

    if corpus_only:
        print(
            "\n--corpus-only: the true-positive side was not measured, so no "
            "threshold is derivable. flag_at stays 1.0."
        )
        return 0

    if not embedding_settings.api_key:
        print(
            "\nVAULT_EMBEDDING_API_KEY is not set — cannot measure the "
            "reference pairs. Re-run with --corpus-only to skip them.",
            file=sys.stderr,
        )
        return 2

    print(f"\nEmbedding {len(REFERENCE_DUPLICATE_PAIRS)} reference pairs...")
    duplicate_scores = await measure_reference_pairs(embedding_settings)
    if duplicate_scores is None:
        print("Reference pair measurement failed; nothing derived.", file=sys.stderr)
        return 1

    print("\nReference duplicate pairs — these MUST flag")
    print("-" * 43)
    for index, score in enumerate(duplicate_scores, start=1):
        print(f"  pair {index}   : {score:.4f}")

    bands = derive_flag_at(distinct_scores, duplicate_scores)
    print("\nDerived threshold")
    print("-" * 17)
    print(f"  floor     : {bands.floor:.4f}  (highest known-distinct pair)")
    print(f"  ceiling   : {bands.ceiling:.4f}  (lowest known-duplicate pair)")
    if bands.recommended is None:
        print("  flag_at   : keep 1.0")
    else:
        print(f"  flag_at   : {bands.recommended}")
    print(f"  because   : {bands.reason}")

    print(
        "\nRecord this run in the model register in "
        "app/vault/docs/embedding-calibration.md before changing DEFAULT_POLICY."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Derive Policy.flag_at for the configured embedding model.",
    )
    parser.add_argument(
        "--corpus-only",
        action="store_true",
        help=(
            "Measure only the existing corpus and skip the paid reference-pair "
            "embeddings. Reports the distribution but derives no threshold."
        ),
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        metavar="N",
        help="How many closest pairs to list. Default: 10.",
    )
    arguments = parser.parse_args()

    if arguments.show < 1:
        parser.error("--show must be at least 1")

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py, conftest.py, and seed_vault_demo.py. No-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.corpus_only, arguments.show),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.corpus_only, arguments.show))


if __name__ == "__main__":
    sys.exit(main())
