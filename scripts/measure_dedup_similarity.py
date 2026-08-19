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
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.env import load_environment
from app.vault.calibration import (
    MINIMUM_SEPARATION,
    REFERENCE_DUPLICATE_PAIRS,
    CalibrationBands,
    derive_flag_at,
)
from app.vault.db import create_vault_engine
from app.vault.domain import DocumentStatus
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embedding_text import assemble_embedding_text
from app.vault.embeddings import EmbeddingError, EmbeddingInputKind, EmbeddingProvider
from app.vault.measurement import percentile
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


async def load_corpus(
    engine: AsyncEngine,
    profile_id: str,
) -> list[tuple[str, list[float]]]:
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


@dataclass(frozen=True, slots=True)
class _EmbeddableRow:
    """Just enough of a document to satisfy ``EmbeddableDocument``.

    A local shape rather than ``VaultDocument`` because the counterfactual needs
    only the five embeddable fields, and selecting the full projection would
    couple this script to changes in columns it never reads.
    """

    title: str
    body: str
    summary: str | None
    tags: tuple[str, ...]
    aliases: tuple[str, ...]


async def load_corpus_documents(engine: AsyncEngine) -> list[_EmbeddableRow]:
    """The same population as ``load_corpus``, as text rather than vectors.

    The counterfactual re-embeds, so it needs the fields
    ``assemble_embedding_text`` reads. Deliberately *not* joined to the
    embeddings table the way ``load_corpus`` is: an unembedded document still
    belongs to the distribution being measured, and the join would narrow the
    sample without saying so.
    """

    statement = (
        select(
            vault_documents.c.title,
            vault_documents.c.body,
            vault_documents.c.summary,
            vault_documents.c.tags,
            vault_documents.c.aliases,
        )
        .where(
            vault_documents.c.status == DocumentStatus.ACTIVE.value,
            readable_path_predicate(),
        )
        .order_by(vault_documents.c.id)
    )
    async with engine.connect() as connection:
        result = await connection.execute(statement)
        return [
            _EmbeddableRow(
                title=row["title"],
                body=row["body"],
                summary=row["summary"],
                tags=tuple(row["tags"] or ()),
                aliases=tuple(row["aliases"] or ()),
            )
            for row in result.mappings()
        ]


async def _embed_documents(
    provider: EmbeddingProvider,
    documents: list[_EmbeddableRow],
    *,
    include_tags: bool,
    batch_size: int = 64,
) -> list[list[float]]:
    """Embed every document one way, in batches."""

    texts = [
        assemble_embedding_text(document, include_tags=include_tags)
        for document in documents
    ]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        embedded = await provider.embed(batch, EmbeddingInputKind.DOCUMENT)
        vectors.extend(list(vector) for vector in embedded)
    return vectors


def _bands_for(
    vectors: list[list[float]],
    titles: list[str],
    duplicate_scores: list[float],
) -> tuple[CalibrationBands, list[tuple[float, str, str]]]:
    pairs = [
        (cosine(vectors[i], vectors[j]), titles[i], titles[j])
        for i in range(len(vectors))
        for j in range(i + 1, len(vectors))
    ]
    bands = derive_flag_at([score for score, _, _ in pairs], duplicate_scores)
    return bands, pairs


async def run_tag_counterfactual(show: int) -> int:
    """Measure the corpus and the reference pairs with and without tags.

    The question this answers is not "do tags help retrieval" — it is whether
    dropping them from the embedding text opens a margin wide enough to make
    semantic dedup calibratable at all. ADR 0016's amendment measured that tags
    inflate the *top* of the corpus distribution, which is precisely the part
    that sets the floor; if that inflation is what closes the band, removing it
    is worth more than tag-weighted ranking.

    Both arms are embedded fresh, including the with-tags arm whose vectors
    already exist in the database. Reading stored vectors for one arm and
    freshly-embedded ones for the other would confound the comparison with
    whatever has changed at the provider since the corpus was imported.
    """

    vault_settings = VaultSettings.from_environment()
    embedding_settings = EmbeddingSettings.from_environment()
    if not embedding_settings.api_key:
        print("VAULT_EMBEDDING_API_KEY is not set.", file=sys.stderr)
        return 1

    print(f"database  : {describe_database(vault_settings.database_url)}")
    print(f"model     : {embedding_settings.model}")

    engine, _observer = create_vault_engine(vault_settings)
    try:
        documents = await load_corpus_documents(engine)
    finally:
        await engine.dispose()

    if len(documents) < 2:
        print(f"Only {len(documents)} document(s).", file=sys.stderr)
        return 1

    titles = [document.title for document in documents]
    tagged = sum(1 for document in documents if document.tags)
    print(f"documents : {len(documents)} ({tagged} carry tags)")
    print(f"pairs     : {len(documents) * (len(documents) - 1) // 2} per arm")

    provider = create_embedding_provider(embedding_settings)
    results: dict[bool, tuple[CalibrationBands, list[tuple[float, str, str]]]] = {}
    try:
        for include_tags in (True, False):
            label = "with tags" if include_tags else "without tags"
            print(f"\nembedding {label}...")
            try:
                vectors = await _embed_documents(
                    provider, documents, include_tags=include_tags
                )
                duplicates = [
                    cosine(
                        list(pair_vectors[0]),
                        list(pair_vectors[1]),
                    )
                    for pair_vectors in [
                        await provider.embed(
                            [
                                assemble_embedding_text(
                                    left, include_tags=include_tags
                                ),
                                assemble_embedding_text(
                                    right, include_tags=include_tags
                                ),
                            ],
                            EmbeddingInputKind.DOCUMENT,
                        )
                        for left, right in REFERENCE_DUPLICATE_PAIRS
                    ]
                ]
            except EmbeddingError as exc:
                print(f"  {type(exc).__name__}", file=sys.stderr)
                return 1
            results[include_tags] = _bands_for(vectors, titles, duplicates)
    finally:
        await provider.aclose()

    print("\n" + "=" * 72)
    print("TAG COUNTERFACTUAL")
    print("=" * 72)
    print(f"\n{'':14}{'floor':>10}{'ceiling':>10}{'margin':>10}   verdict")
    print("-" * 72)
    for include_tags in (True, False):
        bands, _pairs = results[include_tags]
        label = "with tags" if include_tags else "without tags"
        margin = bands.ceiling - bands.floor
        verdict = (
            f"adopt {bands.recommended}"
            if bands.separated
            else "not separable"
        )
        print(
            f"{label:14}{bands.floor:10.4f}{bands.ceiling:10.4f}"
            f"{margin:10.4f}   {verdict}"
        )

    print(f"\nMINIMUM_SEPARATION = {MINIMUM_SEPARATION}")
    for include_tags in (True, False):
        bands, _pairs = results[include_tags]
        label = "With tags" if include_tags else "Without tags"
        print(f"\n{label}: {bands.reason}")

    with_bands, _ = results[True]
    without_bands, without_pairs = results[False]
    delta = (without_bands.ceiling - without_bands.floor) - (
        with_bands.ceiling - with_bands.floor
    )
    print(f"\nRemoving tags changes the margin by {delta:+.4f}.")
    if without_bands.separated and not with_bands.separated:
        print(
            "Removing tags OPENS a usable band. Excluding tags from the "
            "embedding text is now a live proposal — it is an ADR 0013 change "
            "and a full re-embed, so decide it deliberately."
        )
    elif not without_bands.separated:
        print(
            "Removing tags does NOT open a usable band. Tags are not the "
            "blocker, decision 1 in HANDOFF-METADATA.md is settled against "
            "removing them, and flag_at stays 1.0."
        )

    report_closest(without_pairs, show)
    print(
        "\nRecord this run in the register in "
        "app/vault/docs/embedding-calibration.md."
    )
    return 0


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
        "--tag-counterfactual",
        action="store_true",
        help=(
            "Re-embed the whole corpus and the reference pairs twice, with and "
            "without tags in the embedding text, and report the calibration "
            "margin each way. Answers whether tags are what closes the band. "
            "Costs one embedding of every document, twice."
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
    if arguments.tag_counterfactual and arguments.corpus_only:
        parser.error(
            "--tag-counterfactual needs the reference pairs, which --corpus-only "
            "skips; a margin has no ceiling without them"
        )

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py, conftest.py, and seed_vault_demo.py. No-op on Linux/Heroku.
    coroutine = (
        run_tag_counterfactual(arguments.show)
        if arguments.tag_counterfactual
        else run(arguments.corpus_only, arguments.show)
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
