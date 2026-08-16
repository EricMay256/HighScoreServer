"""The ai_read path policy, at the unit level and against the database.

The governance source of truth is ``ai_read`` in ``folders.yml``; excluded
folders are never imported, so ordinarily no row exists to withhold. These
tests cover the second layer — what happens to a row that did land, because a
folder was reclassified after import or something bypassed the importer.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.read_policy import (
    EXCLUDED_PATH_PREFIXES,
    READABLE_PATH_PREFIXES,
    is_readable_path,
)
from app.vault.repository import VaultDocumentRepository
from app.vault.search import VaultSearchRepository
from app.vault.tables import vault_documents
from tests.vault.test_search import vault_service


@pytest.mark.parametrize(
    ("path", "readable"),
    [
        ("Human/17 Concepts/Idempotency.md", True),
        ("Human/03 Projects/HighScoreServer.md", True),
        ("Agent/notes/abc.md", True),
        ("Agent/wiki/page.md", True),
        ("Agent/review/flagged.md", True),
        ("Agent/Promotion Candidates/candidate.md", True),
        # folders.yml has no `Agent/**` catch-all, so an unclassified Agent
        # subfolder resolves to `default: forbidden`. A blanket "Agent/" prefix
        # here would admit it and fail open where governance fails closed.
        ("Agent/experiments/scratch.md", False),
        ("00 Governance/Vault Philosophy.md", True),
        ("Human/01 Inbox/AI/suggestion.md", True),
        # The two the operator named explicitly.
        ("Human/07 People/Alice.md", False),
        ("Human/11 Meetings/2026-07 standup.md", False),
        # Unprocessed capture, and the parent of a readable child.
        ("Human/01 Inbox/raw capture.md", False),
        ("Human/02 Daily/2026-07-29.md", False),
        ("Templates/Concept.md", False),
    ],
)
def test_known_paths_resolve_as_governance_declares(path: str, readable: bool) -> None:
    assert is_readable_path(path) is readable


def test_an_unrecognised_folder_is_unreadable() -> None:
    """Fail closed.

    A folder nobody has classified must not become agent-readable merely
    because no rule excluded it. `folders.yml` defaults `ai_read` to
    ``forbidden`` for the same reason; this asserts the two agree.
    """

    assert is_readable_path("Human/99 Brand New Folder/note.md") is False
    assert is_readable_path("Something Entirely New/note.md") is False
    assert is_readable_path("") is False


def test_no_excluded_prefix_is_stranded_outside_a_readable_one() -> None:
    """`EXCLUDED_PATH_PREFIXES` only means anything inside a readable prefix.

    An exclusion that sits outside every readable prefix is already covered by
    failing closed, so listing it there is dead weight that reads as though it
    were load-bearing.
    """

    for excluded in EXCLUDED_PATH_PREFIXES:
        assert any(
            excluded.startswith(readable) and excluded != readable
            for readable in READABLE_PATH_PREFIXES
        ), f"{excluded!r} is not inside any readable prefix"


def test_search_and_fetch_withhold_a_row_in_an_excluded_folder(
    configure_test_env: None,
) -> None:
    """A row that should never have been imported is still not served.

    Both documents match the query and are ``active``; the only difference is
    the folder. The excluded one must be absent from search and 404 on fetch,
    while remaining loadable by tooling that does not ask for the restriction —
    reconciliation has to see it in order to delete it.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        documents = VaultDocumentRepository()
        search = VaultSearchRepository()
        marker = f"quarklebeam{uuid4().hex[:8]}"
        readable_id = f"policy-ok-{uuid4().hex}"
        excluded_id = f"policy-no-{uuid4().hex}"

        def candidate(document_id: str, vault_path: str) -> NewVaultDocument:
            return NewVaultDocument(
                id=document_id,
                kind=DocumentKind.NOTE,
                vault_path=vault_path,
                status=DocumentStatus.ACTIVE,
                title="Read policy fixture",
                body=f"A note mentioning {marker} exactly once.",
                contributed_by="test:read-policy",
                provenance={"fixture": True},
            )

        try:
            async with service.transaction() as connection:
                await documents.insert(
                    connection,
                    candidate(readable_id, f"Human/17 Concepts/{readable_id}.md"),
                )
                await documents.insert(
                    connection,
                    candidate(excluded_id, f"Human/07 People/{excluded_id}.md"),
                )

            async with service.transaction() as connection:
                hits = await search.lexical_search(
                    connection,
                    query=marker,
                    text_search_config="english",
                    limit=10,
                )
                # Same query, same status, same body — only the folder differs.
                assert [hit.document_id for hit in hits] == [readable_id]

                hydrated = await search.fetch_documents(
                    connection,
                    [readable_id, excluded_id],
                )
                # Hydration re-applies the filter rather than trusting the arm
                # that produced the ids; it is the query that returns bodies.
                assert set(hydrated) == {readable_id}

                assert (
                    await documents.get_by_id(
                        connection, excluded_id, readable_only=True
                    )
                ) is None
                # Unfiltered by default: reconciliation must be able to load a
                # row precisely in order to delete it.
                withheld = await documents.get_by_id(connection, excluded_id)
                assert withheld is not None
                assert withheld.vault_path.startswith("Human/07 People/")

            async with service.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.in_([readable_id, excluded_id])
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_the_sql_predicate_agrees_with_the_python_one(
    configure_test_env: None,
) -> None:
    """One rule, two evaluators — they must not drift.

    ``is_readable_path`` guards application decisions such as import scope;
    ``readable_path_predicate`` guards queries. A divergence between them is
    exactly the bug this policy exists to prevent.
    """

    async def exercise() -> None:
        service, engine = vault_service()
        documents = VaultDocumentRepository()
        paths = (
            "Human/17 Concepts/{}.md",
            "Human/07 People/{}.md",
            "Human/11 Meetings/{}.md",
            "Agent/notes/{}.md",
            "Human/02 Daily/{}.md",
            "Human/99 Unclassified/{}.md",
        )
        created: list[tuple[str, str]] = []

        try:
            async with service.transaction() as connection:
                for template in paths:
                    document_id = f"agree-{uuid4().hex}"
                    vault_path = template.format(document_id)
                    created.append((document_id, vault_path))
                    await documents.insert(
                        connection,
                        NewVaultDocument(
                            id=document_id,
                            kind=DocumentKind.NOTE,
                            vault_path=vault_path,
                            status=DocumentStatus.ACTIVE,
                            title="Predicate agreement fixture",
                            body="Compares the SQL filter against the Python one.",
                            contributed_by="test:read-policy",
                            provenance={"fixture": True},
                        ),
                    )

            async with service.transaction() as connection:
                for document_id, vault_path in created:
                    via_sql = await documents.get_by_id(
                        connection, document_id, readable_only=True
                    )
                    assert (via_sql is not None) is is_readable_path(vault_path), (
                        f"SQL and Python disagree on {vault_path!r}"
                    )

            async with service.transaction() as connection:
                await connection.execute(
                    delete(vault_documents).where(
                        vault_documents.c.id.in_([i for i, _ in created])
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
