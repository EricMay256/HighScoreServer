"""
Measures embedding request latency against the real provider.

Exists to settle one question: the query path's retry budget
(``MAX_EMBEDDING_ATTEMPTS`` and ``DEFAULT_EMBEDDING_TIMEOUT_SECONDS`` in
``app/vault/constants.py``, overridable by ``VAULT_EMBEDDING_TIMEOUT_SECONDS``)
was chosen by reasoning about Heroku's 30s router budget rather than by
measuring this deployment. "Deferred decisions" item 3 in
``app/vault/docs/vault-architecture.md`` records what to measure and how to read
the answer; this script produces the numbers.

How to read the output:

    p99 comfortably under 5s   Three attempts at a 5s timeout is the better
                               configuration: it survives a transient 429 or
                               502, which is the realistic failure, and still
                               fits the router budget.
    p99 at or near 5s          Keep one attempt at 10s. A short timeout would
                               convert slow-but-successful calls into failures
                               and retries into the same wall, which is the one
                               genuinely bad outcome available here.

This calls a paid API. Each sample is one request; the defaults below are
deliberately small. Nothing is written to the database — this script opens no
database connection at all.

Batch mode measures the path the importer will use. One
``VAULT_EMBEDDING_TIMEOUT_SECONDS`` currently covers both the single-query path
and the batch path, and a value chosen for queries may be far too tight for a
full batch of long documents.

Usage:
    Single-query latency:  python -m scripts.measure_embedding_latency
    More samples:          python -m scripts.measure_embedding_latency -n 50
    Batch path:            python -m scripts.measure_embedding_latency --batch 128
    Past the ceiling:      python -m scripts.measure_embedding_latency --timeout 30

Use ``--timeout`` rather than raising ``VAULT_EMBEDDING_TIMEOUT_SECONDS`` when
measuring. A slow response should be recorded rather than truncated into a
timeout — measuring the ceiling you are trying to choose defeats the point — but
since 2026-08-14 the environment variable is validated against the router budget
and refuses anything above 7.3s. The flag applies the override programmatically,
which is the path that validation deliberately leaves open.

Environment variables:
    VAULT_EMBEDDING_API_KEY   Required.
    VAULT_EMBEDDING_MODEL     Optional. Default from EmbeddingSettings.
    VAULT_EMBEDDING_TIMEOUT_SECONDS
                              Optional, and bounded. See ``--timeout`` above.
"""

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import replace

from app.env import load_environment
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embeddings import EmbeddingError, EmbeddingInputKind
from app.vault.measurement import percentile
from app.vault.settings import EmbeddingSettings


# Representative of what the vault's consumer actually sends: an agent asking a
# long natural-language question, not a two-word keyword lookup. Length affects
# tokenization and therefore latency, so short probes would flatter the result.
QUERY_SAMPLES = (
    "How does the vault combine lexical and vector retrieval, and what happens "
    "when the embedding provider is unavailable during a search?",
    "What is the difference between an archived document and a flagged one on "
    "the vault read surface, and which of them resolve by ID?",
    "Why do embeddings live in a join table keyed by profile rather than as a "
    "column on the documents table?",
    "Which decisions require an Alembic revision on the vault lineage rather "
    "than only an application code change?",
    "What is the connection budget calculation for running the leaderboard "
    "pool and the vault pool against one Heroku Postgres plan?",
)

# Long enough to be a realistic document rather than a query, so batch timings
# reflect the importer's workload.
BATCH_DOCUMENT = (
    "The vault is a bounded context hosted inside HighScoreServer. It owns its "
    "own SQLAlchemy Core tables, its own Alembic lineage, and its own ADR "
    "numbering, and it shares no foreign keys, views, triggers, or "
    "transactions with the leaderboard schema. Retrieval is hybrid: a lexical "
    "arm over a persisted tsvector generated column and a vector arm over an "
    "HNSW index, fused by reciprocal rank. The embedding provider is optional "
    "at runtime, and a deployment without one serves lexical-only results and "
    "reports that fact rather than failing the request. "
) * 4


def report(label: str, timings: list[float], failures: int) -> None:
    print(f"\n{label}")
    print("-" * len(label))
    if not timings:
        print("  no successful samples")
        return

    print(f"  samples   : {len(timings)} ok, {failures} failed")
    print(f"  min       : {min(timings):6.3f}s")
    print(f"  p50       : {percentile(timings, 0.50):6.3f}s")
    print(f"  p90       : {percentile(timings, 0.90):6.3f}s")
    print(f"  p99       : {percentile(timings, 0.99):6.3f}s")
    print(f"  max       : {max(timings):6.3f}s")
    print(f"  mean      : {statistics.fmean(timings):6.3f}s")


async def measure(
    samples: int,
    batch_size: int | None,
    timeout_seconds: float | None = None,
) -> int:
    settings = EmbeddingSettings.from_environment()
    if timeout_seconds is not None:
        # Deliberately not via the environment. from_environment validates the
        # configured timeout against the router budget, which is the right rule
        # for a serving process and the wrong one here: measuring where the
        # ceiling should sit requires exceeding the current ceiling. `replace`
        # is the programmatic path the validation intentionally leaves open --
        # the same one a batch backfill uses.
        settings = replace(settings, timeout_seconds=timeout_seconds)
    if not settings.api_key:
        print(
            "VAULT_EMBEDDING_API_KEY is not set - nothing to measure.\n"
            "Set it in the process environment rather than committing it.",
            file=sys.stderr,
        )
        return 2

    provider = create_embedding_provider(settings)
    print(f"provider  : {settings.provider}")
    print(f"model     : {settings.model}")
    print(f"profile   : {settings.profile_id}")
    print(f"timeout   : {settings.timeout_seconds}s")

    timings: list[float] = []
    failures = 0

    try:
        if batch_size is None:
            print(f"\nEmbedding {samples} single queries...")
            for index in range(samples):
                text = QUERY_SAMPLES[index % len(QUERY_SAMPLES)]
                started = time.perf_counter()
                try:
                    await provider.embed([text], EmbeddingInputKind.QUERY)
                except EmbeddingError as exc:
                    # Type only, never the message: an embedding exception can
                    # carry request content.
                    failures += 1
                    print(f"  sample {index + 1}: {type(exc).__name__}")
                    continue
                timings.append(time.perf_counter() - started)
            report("Single-query embedding latency", timings, failures)
        else:
            batch = [BATCH_DOCUMENT] * batch_size
            print(f"\nEmbedding {samples} batches of {batch_size} documents...")
            for index in range(samples):
                started = time.perf_counter()
                try:
                    await provider.embed(batch, EmbeddingInputKind.DOCUMENT)
                except EmbeddingError as exc:
                    failures += 1
                    print(f"  batch {index + 1}: {type(exc).__name__}")
                    continue
                timings.append(time.perf_counter() - started)
            report(
                f"Batch embedding latency ({batch_size} documents)",
                timings,
                failures,
            )
    finally:
        await provider.aclose()

    if not timings:
        return 1

    print(
        "\nRecord these in 'Deferred decisions' item 3 of "
        "app/vault/docs/vault-architecture.md before changing the budget."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure embedding request latency against the real provider.",
    )
    parser.add_argument(
        "-n",
        "--samples",
        type=int,
        default=20,
        help="Number of requests to issue. Default: 20.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Per-attempt timeout for this run only, overriding "
            "VAULT_EMBEDDING_TIMEOUT_SECONDS. Use this rather than raising the "
            "environment variable: that is validated against the router budget "
            "and refuses anything above 7.3s, which is exactly the ceiling you "
            "are trying to measure past."
        ),
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        metavar="SIZE",
        help=(
            "Measure the batch path at this many documents per request "
            "instead of single queries. The importer's default is 128."
        ),
    )
    arguments = parser.parse_args()

    if arguments.samples < 1:
        parser.error("--samples must be at least 1")
    if arguments.batch is not None and arguments.batch < 1:
        parser.error("--batch must be at least 1")

    load_environment()
    # No database connection here, so the SelectorEventLoop dance that
    # run_dev.py and seed_vault_demo.py need does not apply.
    return asyncio.run(
        measure(arguments.samples, arguments.batch, arguments.timeout)
    )


if __name__ == "__main__":
    sys.exit(main())
