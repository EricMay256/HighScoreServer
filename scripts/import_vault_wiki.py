"""
Imports the Stage-A ``Agent/wiki/`` pages into the service as ``kind='wiki'`` rows.

A one-off. The compile path (vault ADR 0027) writes *new* pages; it does not
ingest markdown somebody else already wrote, and the fourteen pages the Stage-A
librarian loop produced are real synthesis worth keeping. This is the bridge, and
once it has run the engine's ``compile plan``/``write``/``finish`` and the
``knowledge-vault`` skill's compile loop can be retired -- at which point ADR
0022's "one writer per tree" is finally true of ``Agent/wiki/``.

Usage:
    Dry run:  python -m scripts.import_vault_wiki --vault-root <path>
    Apply:    python -m scripts.import_vault_wiki --vault-root <path> --apply

Environment variables:
    DATABASE_URL              Required. VAULT_DATABASE_URL takes precedence.
    VAULT_EMBEDDING_API_KEY   Required: pages are embedded, or search cannot
                              return synthesis, which is the point of compiling.

**Historical runs are preserved, not collapsed into one.** The fourteen pages
carry four distinct ``CompileRunID`` values, and each becomes its own
``vault_compile_runs`` row with ``started_at``/``completed_at`` taken from the
pages it produced. Inventing a single import run would make provenance say the
whole wiki was compiled at once, which is not what happened.

**Only the newest run carries a frontier.** ``_last_frontier`` reads the most
recently completed successful run, so the value from Stage A's ``_frontier.yml``
goes on that one alone. It preserves "notes up to here had already been
considered"; putting it on all four would be harmless but false.

**Ids and paths are preserved exactly.** ``vault_path`` keeps the existing slug
rather than being re-derived from the title, because ADR 0010 requires it to
equal the governance scanner's ``rel_path`` and because a re-slugged path would
make the next export delete and recreate every file instead of leaving them
alone. Verified before this was written: all 49 ``SourceIDs`` across the fourteen
pages resolve against the 61 notes on disk, so the August re-import preserved
note identity and the imported provenance is not dangling.

**What the frontmatter parser handles, and what it refuses.** These files were
written by the canonical renderer -- scalars and block sequences, nothing else --
so the inverse is about forty lines and needs no YAML dependency, which is the
same reason ``export.py`` writes but never parses. Anything outside that shape
raises rather than being guessed at.

**Expect every page to plan as stale afterwards, and that is correct.** The
2026-08-21 re-import rewrote all 61 notes, so every source is newer than every
page. The syntheses genuinely are out of date relative to their sources;
backdating to hide that would be a lie told to the planner.
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select

from app.env import load_environment
from app.vault.constants import WIKI_SCHEMA_VERSION
from app.vault.db import create_vault_engine, describe_database
from app.vault.domain import (
    CompileRunState,
    DocumentEmbedding,
    DocumentKind,
    DocumentStatus,
    NewVaultDocument,
)
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.embeddings import EmbeddingInputKind, embed_one
from app.vault.repository import (
    VaultDocumentEmbeddingRepository,
    VaultDocumentRepository,
)
from app.vault.service import AGENT_WIKI_DIRECTORY, VaultTransactionService
from app.vault.settings import EmbeddingSettings, VaultSettings
from app.vault.tables import vault_compile_runs, vault_documents


# Files under `Agent/wiki/` that are not pages. `_index.md` is a generated
# table of contents (the exporter regenerates it); `_frontier.yml` is Stage A's
# own bookkeeping, whose service equivalent is `vault_compile_runs`.
NOT_A_PAGE = {"_index.md", "_frontier.yml"}

_LIST_ITEM = re.compile(r"^  - (.*)$")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*): ?(.*)$")


class FrontmatterError(ValueError):
    """A file this parser will not guess at."""


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """Split canonical frontmatter from a body.

    The inverse of ``export.render_frontmatter``, and only of that: scalars,
    block sequences, and quoted strings. A mapping value, a flow sequence, or a
    multi-line scalar raises -- these files are machine-written, so anything
    outside the shape means the assumption behind this script is wrong and
    guessing would import corrupted metadata silently.
    """

    if not text.startswith("---\n"):
        raise FrontmatterError("file does not open with a frontmatter delimiter")
    _, _, rest = text.partition("---\n")
    block, delimiter, body = rest.partition("\n---\n")
    if not delimiter:
        raise FrontmatterError("frontmatter is not closed")

    metadata: dict[str, Any] = {}
    key: str | None = None
    for line in block.split("\n"):
        if not line.strip():
            continue
        item = _LIST_ITEM.match(line)
        if item is not None:
            if key is None:
                raise FrontmatterError(f"list item before any key: {line!r}")
            metadata.setdefault(key, [])
            if not isinstance(metadata[key], list):
                metadata[key] = []
            metadata[key].append(_scalar(item.group(1)))
            continue
        pair = _KEY_VALUE.match(line)
        if pair is None:
            raise FrontmatterError(f"unparsable frontmatter line: {line!r}")
        key, raw = pair.group(1), pair.group(2).strip()
        if raw == "":
            # Either an empty scalar or the head of a block sequence; the next
            # line decides, and `setdefault` above handles it.
            metadata[key] = None
        elif raw == "[]":
            metadata[key] = []
        else:
            metadata[key] = _scalar(raw)
    return metadata, body


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw.isdigit():
        return int(raw)
    return raw


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class WikiPageFile:
    path: Path
    slug: str
    metadata: dict[str, Any]
    body: str

    @property
    def run_key(self) -> str:
        return str(self.metadata.get("CompileRunID") or "")

    @property
    def compiled_at(self) -> datetime:
        return _timestamp(str(self.metadata["CompiledAt"]))

    @property
    def compiled_by(self) -> str:
        return str(self.metadata.get("CompiledBy") or "agent:librarian")


def read_pages(wiki_directory: Path) -> list[WikiPageFile]:
    pages: list[WikiPageFile] = []
    for path in sorted(wiki_directory.glob("*.md")):
        if path.name in NOT_A_PAGE:
            continue
        metadata, body = parse_note(
            path.read_text(encoding="utf-8").replace("\r\n", "\n")
        )
        for required in ("Title", "CompiledAt", "CompileRunID"):
            if not metadata.get(required):
                raise FrontmatterError(f"{path.name}: missing {required}")
        pages.append(
            WikiPageFile(
                path=path,
                slug=path.stem,
                metadata=metadata,
                body=body,
            )
        )
    return pages


def read_frontier(wiki_directory: Path) -> str | None:
    """Stage A's ``frontier_at``, or None.

    Parsed by hand rather than with a YAML library: the file is three scalar
    lines written by the engine, and this script adds no dependency for it.
    """

    path = wiki_directory / "_frontier.yml"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "frontier_at":
            return value.strip().strip("'\"") or None
    return None


def _build_document(page: WikiPageFile, run_id: UUID, principal_id: str) -> NewVaultDocument:
    """One page as the row it becomes.

    ``compiled_by`` and ``compiled_at`` take the *upstream* values directly,
    because those columns mean exactly what the frontmatter keys mean and
    nothing else is claiming them. ``contributed_by`` does not: ADR 0016 takes
    it from the credential precisely so one principal cannot write under
    another's name, so the original compiler goes in ``origin`` alongside the
    authoring timestamps the database will overwrite with ``now()``.
    """

    return NewVaultDocument(
        id=uuid4().hex,
        kind=DocumentKind.WIKI,
        doc_type=str(page.metadata.get("Type") or "Wiki Page"),
        # The existing slug, never re-derived. ADR 0010 makes vault_path the
        # scanner's rel_path, and a re-slugged one would make the next export
        # delete and recreate all fourteen files.
        vault_path=f"{AGENT_WIKI_DIRECTORY}{page.slug}.md",
        status=DocumentStatus.ACTIVE,
        doc_status=str(page.metadata.get("Status") or "Current"),
        title=str(page.metadata["Title"]),
        summary=(
            str(page.metadata["Summary"]) if page.metadata.get("Summary") else None
        ),
        body=page.body,
        tags=tuple(_as_list(page.metadata.get("tags"))),
        aliases=tuple(_as_list(page.metadata.get("aliases"))),
        source_ids=tuple(_as_list(page.metadata.get("SourceIDs"))),
        related_ids=tuple(_as_list(page.metadata.get("Related"))),
        contributed_by=f"agent:{principal_id}",
        provenance={"principal_id": principal_id, "imported_from": page.path.name},
        origin={
            "author": page.compiled_by,
            "created_at": str(page.metadata.get("CreatedAt") or ""),
            "updated_at": str(page.metadata.get("LastUpdated") or ""),
            "run_id": page.run_key,
        },
        schema_version=int(page.metadata.get("SchemaVersion") or WIKI_SCHEMA_VERSION),
        compile_run_id=run_id,
        compiled_by=page.compiled_by,
        compiled_at=page.compiled_at,
    )


async def run_import(
    wiki_directory: Path,
    principal_id: str,
    apply: bool,
) -> int:
    pages = read_pages(wiki_directory)
    if not pages:
        print(f"No wiki pages found under {wiki_directory}", file=sys.stderr)
        return 1

    by_run: dict[str, list[WikiPageFile]] = {}
    for page in pages:
        by_run.setdefault(page.run_key, []).append(page)
    frontier = read_frontier(wiki_directory)
    newest_run = max(
        by_run, key=lambda key: max(p.compiled_at for p in by_run[key])
    )

    print(f"{len(pages)} page(s) across {len(by_run)} historical compile run(s)")
    for key in sorted(by_run, key=lambda k: min(p.compiled_at for p in by_run[k])):
        group = by_run[key]
        span = min(p.compiled_at for p in group)
        marker = "  <- carries the frontier" if key == newest_run else ""
        print(f"  {key}  {len(group):>2} page(s)  {span.isoformat()}{marker}")
    if frontier:
        print(f"frontier_at from Stage A: {frontier}")

    if not apply:
        # Says "no database" rather than only "nothing was written",
        # because the difference matters: this returns before an engine is
        # even built, so a dry run passing tells you the *files* parse and
        # nothing whatsoever about the database it would write to. Reading
        # it as a green light for the target has already cost an afternoon.
        print("\nDry run. No database was contacted and nothing was written. Re-run with --apply.")
        return 0

    settings = replace(VaultSettings.from_environment(), enabled=True)
    # Before the work, not after: this writes to whatever the environment
    # resolved to, and the operator is the only one who knows whether that
    # is the database they meant.
    print(f"database   : {describe_database(settings.database_url)}")
    embedding = EmbeddingSettings.from_environment()
    if not embedding.api_key:
        # Refused rather than degraded. The read path may fall back to lexical
        # search; a page written without an embedding would simply never be
        # returned by the vector arm, which defeats compiling it.
        print(
            "No embedding provider configured. A page nobody can find is not a "
            "page; set VAULT_EMBEDDING_API_KEY.",
            file=sys.stderr,
        )
        return 1
    provider = create_embedding_provider(embedding)

    engine, observer = create_vault_engine(settings)
    transactions = VaultTransactionService(engine, observer)
    documents = VaultDocumentRepository()
    embeddings = VaultDocumentEmbeddingRepository()

    try:
        async with transactions.transaction() as connection:
            existing = await connection.execute(
                select(vault_documents.c.vault_path).where(
                    vault_documents.c.kind == DocumentKind.WIKI.value
                )
            )
            taken = set(existing.scalars())
        collisions = [p.slug for p in pages if f"{AGENT_WIKI_DIRECTORY}{p.slug}.md" in taken]
        if collisions:
            # Refuse rather than upsert. This script exists to run once, and a
            # second run over a corpus that already holds these pages would
            # either duplicate them or silently overwrite compiled content.
            print(
                f"Refusing: {len(collisions)} page(s) already exist as rows "
                f"({', '.join(sorted(collisions)[:3])}...). This import runs once.",
                file=sys.stderr,
            )
            return 1

        # Embed before the transaction that writes, for the reason the write
        # path does: an embedding call is a third-party round trip and holding
        # a pooled connection across fourteen of them is the mistake this
        # package keeps naming.
        prepared: list[tuple[NewVaultDocument, tuple[float, ...], bytes]] = []
        run_ids = {key: uuid4() for key in by_run}
        for page in pages:
            document = _build_document(page, run_ids[page.run_key], principal_id)
            text = assemble_embedding_text(document)
            digest = embedding_text_digest(text)
            vector = await embed_one(provider, text, EmbeddingInputKind.DOCUMENT)
            prepared.append((document, vector, digest))
            print(f"  embedded {page.slug}")

        async with transactions.transaction() as connection:
            for key, run_id in run_ids.items():
                group = by_run[key]
                started = min(p.compiled_at for p in group)
                completed = max(p.compiled_at for p in group)
                await connection.execute(
                    insert(vault_compile_runs).values(
                        id=run_id,
                        compiler_principal_id=group[0].compiled_by,
                        state=CompileRunState.SUCCEEDED.value,
                        started_at=started,
                        completed_at=completed,
                        input_frontier={},
                        output_frontier=(
                            {"frontier_at": frontier}
                            if frontier and key == newest_run
                            else {}
                        ),
                    )
                )
            for document, vector, digest in prepared:
                stored = await documents.insert(connection, document)
                await embeddings.upsert(
                    connection,
                    DocumentEmbedding(
                        document_id=stored.id,
                        profile_id=provider.profile_id,
                        vector=vector,
                        text_sha256=digest,
                    ),
                )
    finally:
        await engine.dispose()

    print(f"\nImported {len(prepared)} page(s) under {len(run_ids)} compile run(s).")
    print(
        "Next: add 'Agent/wiki/' to CORPUS_OWNED_PATH_PREFIXES, then dry-run "
        "the exporter and confirm a zero-line diff before pruning."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Stage-A Agent/wiki pages into the vault service.",
    )
    parser.add_argument(
        "--vault-root",
        required=True,
        type=Path,
        help="Path to the Vault directory containing Agent/wiki/.",
    )
    parser.add_argument(
        "--principal",
        default="wiki-importer",
        help="Principal recorded as contributed_by (default: wiki-importer).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write. Without it this reports what it would do and exits.",
    )
    arguments = parser.parse_args()
    load_environment()

    wiki = arguments.vault_root / "Agent" / "wiki"
    if not wiki.is_dir():
        parser.error(f"no Agent/wiki directory under {arguments.vault_root}")

    coroutine = run_import(wiki, arguments.principal, arguments.apply)

    # See scripts/issue_vault_credential.py; a no-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coroutine)


if __name__ == "__main__":
    sys.exit(main())
