"""
Seeds a small demo corpus into the vault with real embeddings, then runs one
hybrid search against it.

This is a fixture loader for onboarding and manual verification, NOT a
production backfill. A real backfill has to be resumable, batched, and
checkpointed, and must not re-embed documents that already have a row under the
target profile. This re-embeds all four documents every run.

The corpus is deliberately chosen so the two retrieval arms behave differently:
one document uses a distinctive keyword (HNSW), one expresses a related idea in
entirely different words, one shares an incidental term with the query, and one
is unrelated. Running it prints each document's lexical rank, vector rank, and
fused score, which makes it visible when only one arm is contributing.

Every row it writes has an id prefixed "demo-vault-", and --clean removes
exactly those and nothing else. It prints the target database before writing.

Usage:
    Seed and search:    python -m scripts.seed_vault_demo
    Remove the corpus:  python -m scripts.seed_vault_demo --clean

Environment variables:
    DATABASE_URL              Required. Vault schema must already be migrated.
    VAULT_EMBEDDING_API_KEY   Required to seed. Not needed for --clean.
    VAULT_TEXT_SEARCH_CONFIG  Optional. Must match what the schema was built
                              with. Default: english.
"""

import argparse
import asyncio
from dataclasses import replace
import sys

from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app.env import load_environment
from app.vault.db import create_vault_engine
from app.vault.domain import (
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
    VectorSearchStatus,
)
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embeddings import EmbeddingInputKind
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
)
from app.vault.service import (
    HybridSearchOutcome,
    VaultSearchService,
    VaultTransactionService,
)
from app.vault.settings import EmbeddingSettings, VaultSettings
from app.vault.tables import vault_documents


ID_PREFIX = "demo-vault-"

DEMO_QUERY = "how do I make vector similarity search faster?"

# (suffix, title, body)
CORPUS: tuple[tuple[str, str, str], ...] = (
    (
        "hnsw",
        "HNSW index tuning",
        "Raising m and ef_construction on an HNSW index improves recall at the "
        "cost of build time and memory. Query-time ef_search trades latency "
        "for accuracy.",
    ),
    (
        "paraphrase",
        "Making similarity lookups faster",
        "Approximate nearest neighbour graphs let you avoid scanning every "
        "stored vector. You accept a small chance of missing the true closest "
        "match in exchange for far lower query latency.",
    ),
    (
        "lexical-decoy",
        "Weekly retrospective",
        "We discussed latency in the checkout flow and agreed to revisit the "
        "caching strategy next sprint.",
    ),
    (
        "unrelated",
        "Sourdough starter notes",
        "Feed the starter twice daily at room temperature. Discard half before "
        "each feeding or it outgrows the jar.",
    ),
)


def describe_database(url: str) -> str:
    """Host, port, and database name only — never the credential."""

    parsed = make_url(url)
    return f"{parsed.host}:{parsed.port}/{parsed.database}"


async def remove_demo_documents(transactions: VaultTransactionService) -> int:
    """Delete every demo document. Embeddings follow via ON DELETE CASCADE."""

    async with transactions.transaction() as connection:
        result = await connection.execute(
            delete(vault_documents).where(vault_documents.c.id.like(f"{ID_PREFIX}%"))
        )
        return result.rowcount or 0


def print_results(outcome: HybridSearchOutcome) -> None:
    """Render the fused ranking so a silent arm is obvious at a glance."""

    print(f"\n{'rank':<6}{'lex':<6}{'vec':<6}{'rrf':<10}title")
    print("-" * 70)
    for position, result in enumerate(outcome.results, start=1):
        lexical = result.lexical_rank if result.lexical_rank is not None else "-"
        vector = result.vector_rank if result.vector_rank is not None else "-"
        print(
            f"{position:<6}{lexical:<6}{vector:<6}"
            f"{result.score:<10.5f}{result.document.title}"
        )


async def run(clean_only: bool) -> int:
    vault_settings = replace(VaultSettings.from_environment(), enabled=True)
    print(f"database   : {describe_database(vault_settings.database_url)}")

    engine, observer = create_vault_engine(vault_settings)
    transactions = VaultTransactionService(engine, observer)

    try:
        if clean_only:
            removed = await remove_demo_documents(transactions)
            print(f"\nRemoved {removed} demo document(s).")
            return 0

        embedding_settings = EmbeddingSettings.from_environment()
        print(f"profile    : {embedding_settings.profile_id}")
        if not embedding_settings.api_key:
            print("\nVAULT_EMBEDDING_API_KEY is not set - nothing to embed.")
            return 2

        provider = create_embedding_provider(embedding_settings)
        try:
            # Start from a clean slate so re-running is idempotent.
            await remove_demo_documents(transactions)

            bodies = [f"{title}\n\n{body}" for _suffix, title, body in CORPUS]
            print(f"\nEmbedding {len(bodies)} documents...")
            vectors = await provider.embed(bodies, EmbeddingInputKind.DOCUMENT)

            documents = VaultDocumentRepository()
            embeddings = VaultDocumentEmbeddingRepository()
            async with transactions.transaction() as connection:
                for (suffix, title, body), vector in zip(
                    CORPUS, vectors, strict=True
                ):
                    document_id = f"{ID_PREFIX}{suffix}"
                    await documents.insert(
                        connection,
                        NewVaultDocument(
                            id=document_id,
                            kind=DocumentKind.NOTE,
                            status=DocumentStatus.ACTIVE,
                            title=title,
                            body=body,
                            contributed_by="script:seed_vault_demo",
                            provenance={"demo": True},
                        ),
                    )
                    await embeddings.upsert(
                        connection,
                        DocumentEmbedding(
                            document_id=document_id,
                            profile_id=provider.profile_id,
                            vector=vector,
                        ),
                    )
            print(f"Seeded {len(CORPUS)} documents under {provider.profile_id}")

            search = VaultSearchService(
                transactions=transactions,
                provider=provider,
                text_search_config=vault_settings.text_search_config,
            )
            print(f"\nquery      : {DEMO_QUERY!r}")
            outcome = await search.search(DEMO_QUERY, limit=10)
            print(f"status     : {outcome.vector_status.value}")
            if outcome.vector_status is not VectorSearchStatus.USED:
                print("\nVector arm did not run - results below are lexical only.")

            print_results(outcome)

            if all(result.lexical_rank is None for result in outcome.results):
                # Uncommon since ADR 0007 made the arm disjunctive: it now
                # takes a query sharing no stemmed term with any document.
                # Saying so beats letting the empty column pass for agreement.
                print(
                    "\nNote: no lexical matches - the fusion below is "
                    "vector-only.\n      The lexical arm ORs the query's terms "
                    "(ADR 0007), so this means\n      the query shares no "
                    "stemmed word with any document."
                )

            print("\nRe-run with --clean to remove these documents.")
        finally:
            await provider.aclose()
    finally:
        await engine.dispose()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed a demo vault corpus with real embeddings."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the demo documents and exit without embedding anything.",
    )
    arguments = parser.parse_args()

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. run_dev.py
    # and conftest.py handle this the same way. No-op on Linux/Heroku, where
    # SelectorEventLoop is already the default.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.clean),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.clean))


if __name__ == "__main__":
    sys.exit(main())
