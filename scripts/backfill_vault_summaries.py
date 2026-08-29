"""
Backfills the `summary` that most existing notes were contributed without.

Measured 2026-08-26: **3 of 70 notes carry a summary, against 14 of 15 wiki
pages.** `summary` joins the embedding text and the `search_vector` at weight B
(ADR 0013) and is the search preview since ADR 0031, so an absent one costs a
ranking signal and leaves search showing a derived lead extract where an
authored precis belongs. ADR 0035 stops the gap reopening at the write path;
this closes the notes already in the corpus.

**The summaries are written by an agent, not derived by this script, and that
is the whole design.** A derived summary would be an extract of the opening
paragraph -- which is exactly what `snippet.lead_snippet` already produces at
query time, for free, without occupying the authored field. Writing it into
`summary` would add no information, would cost an embedding call per note to
say the same words twice, and would destroy the one signal that distinguishes
"nobody has described this note" from "this note is described by its opening".
Every note would then look summarized and none would be.

So this follows the split ADR 0027 draws for compilation: **the service plans
the run and the agent writes it.**

    1. python -m scripts.backfill_vault_summaries
           Report which notes lack a summary. Reads nothing else.

    2. python -m scripts.backfill_vault_summaries --emit work.json
           Write a work file: one entry per undescribed note, carrying the
           title and body an agent needs to write a precis, and an empty
           `summary` for it to fill in.

    3. python -m scripts.backfill_vault_summaries --from work.json
           Dry run. Validates every authored summary and reports what would be
           written, without writing.

    4. python -m scripts.backfill_vault_summaries --from work.json --apply
           Embed and write, in one transaction.

**This deliberately does not run the dedup gate**, unlike every other write
path in the vault. The gate exists to stop the corpus accreting near-duplicate
*notes*, and adding a precis to an existing note cannot create one -- the note
was already there and is not being multiplied. What the gate could do is refuse
a legitimate backfill because some unrelated pair happens to sit near the
threshold, which would be a refusal the operator cannot act on and cannot
route around. `vault_set_summary` (ADR 0035) does run the gate, because that is
an agent-facing path under an agent's own credential; this is an operator
running a one-off repair against the database directly.

**A summary changes the embedding text, so this is a re-embed.** Both halves
move together in one transaction: the vector and `embedded_text_sha256`. That
is not tidiness -- nothing in this codebase repairs a stale hash after the
fact, so a run that wrote the summary and skipped the embedding would leave a
vector permanently describing text nobody embedded, and the staleness check on
the update path would never notice because the hash would agree with the wrong
text. The cost is one embedding call per note, batched: at corpus size that is
fractions of a cent, and the reason to care about it is correctness, not spend.

Nothing already summarized is touched. `summary IS NULL` is in the predicate as
well as in the plan, so a second run writes nothing and a note summarized by an
agent between the emit and the apply is left as the agent wrote it.

**A summary is only valid for the text it was written about.** Authoring takes
as long as it takes and the note stays writable throughout, so each emitted
entry records the `content_revision` its body was read at, and the apply refuses
any entry whose note has moved since. That is a separate question from whether
the vector is consistent -- a precis of the old note embedded with the new one
produces a row that agrees with itself and misdescribes the document, in the two
fields retrieval actually reads. A refused entry needs re-emitting and
re-authoring; rerunning the same file will refuse it again, which is the point.

Usage:
    Report:   python -m scripts.backfill_vault_summaries
    Emit:     python -m scripts.backfill_vault_summaries --emit work.json
    Dry run:  python -m scripts.backfill_vault_summaries --from work.json
    Write:    python -m scripts.backfill_vault_summaries --from work.json --apply

Environment variables:
    DATABASE_URL              Required. VAULT_DATABASE_URL takes precedence.
    VAULT_EMBEDDING_API_KEY   Required for --apply; the re-embed needs it.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy import text as text_sql
from sqlalchemy.ext.asyncio import AsyncEngine

from app.env import load_environment
from app.vault.constants import CORPUS_LOCK_KEY
from app.vault.db import create_vault_engine, describe_database
from app.vault.domain import DocumentKind, DocumentStatus
from app.vault.embedding_runtime import create_embedding_provider
from app.vault.embedding_text import assemble_embedding_text, embedding_text_digest
from app.vault.embeddings import EmbeddingError, EmbeddingInputKind, EmbeddingProvider
from app.vault.governance import validate
from app.vault.settings import EmbeddingSettings, VaultSettings
from app.vault.tables import vault_document_embeddings, vault_documents


# The largest summary the write boundary accepts, mirrored from
# `VaultSetSummaryRequest`. Checked here so a work file authored too long fails
# in the dry run rather than at the last statement of the apply.
MAX_SUMMARY_CHARS = 2_000

# One provider call per batch rather than per note. 64 matches
# `measure_dedup_similarity`, which is the only other script that embeds in
# bulk; there is no reason for the two to disagree.
EMBED_BATCH_SIZE = 64

# The work-file shape. 1 is the original, which carried no `content_revision`
# per entry; 2 adds it. Version 1 files are refused rather than assumed
# current, because the whole point of the field is that its absence cannot be
# distinguished from "the note has not moved" -- and guessing that wrongly
# writes a summary describing a note that no longer exists in that form.
WORK_FILE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Undescribed:
    """One note with no summary, and what an author needs to write one."""

    document_id: str
    vault_path: str
    title: str
    body: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    contributed_by: str
    # The revision this snapshot was read at. The vector is computed from the
    # fields above, outside any transaction, so this is what proves at write
    # time that they are still the note's fields.
    content_revision: int


@dataclass(frozen=True)
class Authored:
    """One note's authored summary, paired with the row it belongs to."""

    note: Undescribed
    summary: str


async def load_undescribed(engine: AsyncEngine) -> list[Undescribed]:
    """Every active note carrying no summary, oldest first.

    Notes only. A wiki page's summary is the compiler's output (ADR 0027), so
    backfilling one by hand would put an operator's prose in a field the next
    compile run overwrites -- and 14 of the 15 pages have one already.

    Active only. An archived note is still readable (ADR 0008) but is not what
    search ranks for, and a flagged one is awaiting adjudication on content
    that may not survive it; neither is worth an embedding call.

    Ordered by `created_at` so two runs plan the same corpus in the same order
    and their work files diff cleanly.
    """

    statement = (
        select(
            vault_documents.c.id,
            vault_documents.c.vault_path,
            vault_documents.c.title,
            vault_documents.c.body,
            vault_documents.c.tags,
            vault_documents.c.aliases,
            vault_documents.c.contributed_by,
            vault_documents.c.content_revision,
        )
        .where(vault_documents.c.kind == DocumentKind.NOTE.value)
        .where(vault_documents.c.status == DocumentStatus.ACTIVE.value)
        .where(vault_documents.c.summary.is_(None))
        .order_by(vault_documents.c.created_at)
    )
    async with engine.connect() as connection:
        result = await connection.execute(statement)
        return [
            Undescribed(
                document_id=row["id"],
                vault_path=row["vault_path"],
                title=row["title"],
                body=row["body"],
                tags=tuple(row["tags"] or ()),
                aliases=tuple(row["aliases"] or ()),
                contributed_by=row["contributed_by"],
                content_revision=row["content_revision"],
            )
            for row in result.mappings()
        ]


async def count_notes(engine: AsyncEngine) -> tuple[int, int]:
    """(active notes, of which summarized). Context for the report."""

    statement = select(
        func.count(),
        func.count(vault_documents.c.summary),
    ).where(
        vault_documents.c.kind == DocumentKind.NOTE.value,
        vault_documents.c.status == DocumentStatus.ACTIVE.value,
    )
    async with engine.connect() as connection:
        total, described = (await connection.execute(statement)).one()
    return int(total), int(described)


def emit(notes: list[Undescribed], path: Path) -> None:
    """Write the work file an agent fills in.

    Carries the whole body rather than an excerpt. A precis of a note is worth
    having only if it was written against the whole note, and an author handed
    the first paragraph would produce the lead extract this script exists not
    to produce.

    Each entry records the `content_revision` its body was read at, which is
    what `read_work_file` compares against the corpus later. Authoring takes as
    long as it takes, and the note is writable throughout.
    """

    payload = {
        "schema_version": WORK_FILE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "instructions": (
            "Write `summary` for each entry: two or three sentences saying "
            "what the note establishes. Summarize the whole note, not its "
            "opening -- search already falls back to an extract of the opening "
            "paragraph, so restating it adds nothing. Leave `summary` empty to "
            "skip a note; the run will report it as skipped rather than fail."
        ),
        "notes": [
            {
                "id": note.document_id,
                # The revision the `body` below was read at, and therefore the
                # one the author's summary will describe. Compared against the
                # note at apply time: a precis of a note that has since been
                # rewritten is wrong in a way no vector check can see, because
                # the vector would be consistent with the new text and the
                # sentence would still be about the old.
                "content_revision": note.content_revision,
                "vault_path": note.vault_path,
                "title": note.title,
                "summary": "",
                "body": note.body,
            }
            for note in notes
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_work_file(
    path: Path,
    notes: list[Undescribed],
) -> tuple[list[Authored], list[str], list[str]]:
    """Pair authored summaries with the rows they name.

    Returns (work, skipped, problems), and the split between the last two
    is the exit code. **Skipped is not a failure.** An id that no longer
    resolves means the note gained a summary between the emit and the apply --
    an agent used vault_set_summary, or an earlier run of this script
    succeeded -- so leaving it alone is the correct outcome, and a re-run of a
    finished backfill exits 0 rather than reporting itself as broken. A problem
    is something the author has to change.

    Re-reads the corpus rather than trusting the file's copy of it: the body in
    a work file is a snapshot, and the summary has to be embedded alongside
    whatever the note says *now*. An id in the file that is no longer
    undescribed is reported and skipped.

    **Re-reading makes the vector right and says nothing about the sentence.**
    The author wrote its precis against the body this file exported. If the
    note was rewritten in between, pairing that precis with the current text
    produces a row whose vector and digest agree with each other perfectly and
    whose summary describes a document that no longer exists -- and since the
    summary is both the search preview and an embedded ranking signal, that is
    a note misrepresenting itself in the two places retrieval looks. No
    consistency check downstream can catch it, because nothing downstream knows
    what the summary was written about. So the emitted `content_revision` is
    compared here, and a mismatch is a problem rather than a skip: it needs a
    person to re-author, not a rerun.
    """

    by_id = {note.document_id: note for note in notes}
    document = json.loads(path.read_text(encoding="utf-8"))
    work: list[Authored] = []
    skipped: list[str] = []
    problems: list[str] = []

    version = document.get("schema_version")
    if version != WORK_FILE_SCHEMA_VERSION:
        return (
            [],
            [],
            [
                f"work file is schema_version {version!r}, not "
                f"{WORK_FILE_SCHEMA_VERSION}. Files written before this "
                "version do not record which revision each summary was "
                "authored against, and that cannot be reconstructed. Re-emit "
                "and re-author."
            ],
        )

    for entry in document.get("notes", []):
        document_id = entry.get("id")
        summary = (entry.get("summary") or "").strip()
        if not summary:
            continue
        note = by_id.get(document_id)
        if note is None:
            skipped.append(
                f"{document_id}: already summarized, or no longer an active note"
            )
            continue
        authored_against = entry.get("content_revision")
        if authored_against is None:
            problems.append(
                f"{document_id}: entry records no content_revision, so there "
                "is no way to tell what this summary was written about. "
                "Re-emit and re-author it."
            )
            continue
        if authored_against != note.content_revision:
            problems.append(
                f"{document_id}: authored against revision {authored_against}, "
                f"but the note is now at {note.content_revision}. The summary "
                "describes text the note no longer has. Re-emit and re-author "
                "it."
            )
            continue
        if len(summary) > MAX_SUMMARY_CHARS:
            problems.append(
                f"{document_id}: summary is {len(summary)} chars, "
                f"over the {MAX_SUMMARY_CHARS} the write boundary accepts"
            )
            continue
        work.append(Authored(note=note, summary=summary))

    return work, skipped, problems


@dataclass(frozen=True)
class _Embeddable:
    """The note as it would be with the summary filled in.

    Carries exactly what `assemble_embedding_text` and `governance.validate`
    read between them -- the five embedding fields plus `contributed_by`, which
    only the validator wants. One shape rather than two because both functions
    take a structural protocol, and a second near-identical dataclass would be
    one more place for the embedding fields to drift.
    """

    title: str
    body: str
    summary: str | None
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    contributed_by: str


def embeddable(item: Authored) -> _Embeddable:
    return _Embeddable(
        title=item.note.title,
        body=item.note.body,
        summary=item.summary,
        tags=item.note.tags,
        aliases=item.note.aliases,
        contributed_by=item.note.contributed_by,
    )


def governance_problems(work: list[Authored]) -> list[str]:
    """Validation failures, as the write boundary would report them."""

    problems: list[str] = []
    for item in work:
        errors = validate(embeddable(item))
        if errors:
            problems.append(f"{item.note.document_id}: {'; '.join(errors)}")
    return problems


async def embed_all(
    provider: EmbeddingProvider,
    work: list[Authored],
) -> list[tuple[Authored, tuple[float, ...], bytes]]:
    """Embed every summarized note, batched. Returns (item, vector, digest).

    Every text goes through `assemble_embedding_text` and every digest through
    `embedding_text_digest`, which is what keeps the stored hash a hash of the
    text that was actually embedded. Building either by hand here would be the
    one bug this column cannot survive.
    """

    texts = [assemble_embedding_text(embeddable(item)) for item in work]
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        embedded = await provider.embed(batch, EmbeddingInputKind.DOCUMENT)
        vectors.extend(tuple(vector) for vector in embedded)

    return [
        (item, vector, embedding_text_digest(text))
        for item, vector, text in zip(work, vectors, texts, strict=True)
    ]


async def write(
    engine: AsyncEngine,
    embedded: list[tuple[Authored, tuple[float, ...], bytes]],
    profile_id: str,
) -> tuple[list[str], list[Undescribed]]:
    """Write every summary and its vector in one transaction.

    Returns the ids written and the snapshots that went stale.

    One transaction for the whole run, not one per note: a partial backfill is
    harder to reason about than either outcome, and the run is small enough
    that holding it costs nothing. Every embedding call has already happened by
    the time this opens, so no provider latency is held inside the transaction.

    Every condition the plan selected on is repeated in the predicate, because
    the plan was read before the embedding calls and anything may have
    committed in between:

    - `summary IS NULL` -- `vault_set_summary` may have landed on the same note.
      An agent's own precis is the better one, and this is what loses that race
      gracefully instead of overwriting it.
    - `content_revision` -- the note's title, body or tags may have changed.
      The vector was computed from the snapshot, so writing it against newer
      content would pair the note with an index of text it no longer contains,
      and the stored digest would agree with the vector rather than with the
      row, leaving nothing able to detect it.
    - `kind` and `status` -- the note may have been retired or archived since
      the plan, and neither is worth a summary.

    A row failing any of them is skipped and reported, not written. The
    operator reruns; a rerun re-reads and re-embeds whatever it skipped.

    The corpus advisory lock is held for the writes so this operator path
    serializes with the service's writers like every other one. The lock is not
    sufficient on its own -- the embedding happened before it was acquired,
    which is exactly what the revision check covers.
    """

    written: list[str] = []
    stale: list[Undescribed] = []
    async with engine.begin() as connection:
        await connection.execute(
            text_sql("SELECT pg_advisory_xact_lock(:key)"),
            {"key": CORPUS_LOCK_KEY},
        )
        for item, vector, digest in embedded:
            result = await connection.execute(
                update(vault_documents)
                .where(vault_documents.c.id == item.note.document_id)
                .where(vault_documents.c.summary.is_(None))
                .where(
                    vault_documents.c.content_revision
                    == item.note.content_revision
                )
                .where(vault_documents.c.kind == DocumentKind.NOTE.value)
                .where(vault_documents.c.status == DocumentStatus.ACTIVE.value)
                .values(
                    summary=item.summary,
                    updated_at=func.now(),
                    content_revision=vault_documents.c.content_revision + 1,
                )
                .returning(vault_documents.c.id)
            )
            if result.scalar_one_or_none() is None:
                stale.append(item.note)
                continue

            # Update-then-insert rather than an upsert helper, because this
            # script must not depend on the repository layer's transaction
            # discipline while holding its own connection.
            updated = await connection.execute(
                update(vault_document_embeddings)
                .where(
                    vault_document_embeddings.c.document_id == item.note.document_id,
                    vault_document_embeddings.c.profile_id == profile_id,
                )
                .values(
                    embedding=list(vector),
                    embedded_text_sha256=digest,
                    embedded_at=func.now(),
                )
                .returning(vault_document_embeddings.c.document_id)
            )
            if updated.scalar_one_or_none() is None:
                await connection.execute(
                    vault_document_embeddings.insert().values(
                        document_id=item.note.document_id,
                        profile_id=profile_id,
                        embedding=list(vector),
                        embedded_text_sha256=digest,
                    )
                )
            written.append(item.note.document_id)
    return written, stale


def _report_plan(
    notes: list[Undescribed],
    total: int,
    described: int,
    *,
    suggest_emit: bool,
) -> None:
    print(f"active notes      : {total}")
    print(f"carrying a summary: {described}")
    print(f"undescribed       : {len(notes)}")
    if not notes:
        print("\nNothing to backfill.")
        return
    print()
    for note in notes:
        print(f"  {note.document_id}  {note.title}")
    if suggest_emit:
        print("\nRe-run with --emit <path> to write a work file for an agent to fill in.")


async def run(
    emit_path: Path | None,
    work_path: Path | None,
    apply: bool,
) -> int:
    vault_settings = VaultSettings.from_environment()
    print(f"database: {describe_database(vault_settings.database_url)}")

    engine, _observer = create_vault_engine(vault_settings)
    try:
        notes = await load_undescribed(engine)
        total, described = await count_notes(engine)

        if work_path is None:
            _report_plan(
                notes, total, described, suggest_emit=emit_path is None
            )
            if emit_path is not None:
                emit(notes, emit_path)
                print(f"\nWrote {len(notes)} entries to {emit_path}.")
            return 0

        work, skipped, problems = read_work_file(work_path, notes)
        problems += governance_problems(work)

        print(f"undescribed       : {len(notes)}")
        print(f"summaries authored: {len(work)}")
        if skipped:
            print(f"\n{len(skipped)} already settled, and left alone:")
            for item in skipped:
                print(f"  {item}")
        if problems:
            print(f"\n{len(problems)} problem(s) to fix in the work file:")
            for problem in problems:
                print(f"  {problem}")

        if not work:
            print("\nNothing to write.")
            return 1 if problems else 0

        if not apply:
            print()
            for item in work:
                print(f"  {item.note.document_id}  {item.summary[:90]}")
            print("\nDry run. Re-run with --apply to embed and write.")
            return 0

        embedding_settings = EmbeddingSettings.from_environment()
        if not embedding_settings.api_key:
            print(
                "VAULT_EMBEDDING_API_KEY is not set, and a summary changes the "
                "embedding text. Refusing to write a stale vector.",
                file=sys.stderr,
            )
            return 1

        provider = create_embedding_provider(embedding_settings)
        try:
            print(f"\nembedding {len(work)} note(s) with {embedding_settings.model}...")
            embedded = await embed_all(provider, work)
            provider_profile_id = provider.profile_id
        except EmbeddingError as exc:
            # Type only, never the message: an embedding exception can carry
            # the note body.
            print(
                f"Embedding failed ({type(exc).__name__}); nothing was written.",
                file=sys.stderr,
            )
            return 1
        finally:
            await provider.aclose()

        # The provider's profile, not the settings' -- the row records which
        # profile produced these vectors, and the provider is what produced
        # them. The two agree today; using the one that cannot disagree is
        # what keeps a future adapter that resolves its own profile honest.
        written, stale = await write(engine, embedded, provider_profile_id)
        print(f"wrote {len(written)} summar{'y' if len(written) == 1 else 'ies'}.")
        skipped = len(work) - len(written)
        if skipped:
            print(f"{skipped} were not written, and were left exactly as they are.")
        if stale:
            # Named individually because a rerun is the fix and the operator has
            # to know there is one to do. The row is not damaged -- nothing was
            # written to it -- but its authored summary is still only in the
            # work file.
            print(
                f"\n{len(stale)} changed after this run embedded them, so writing "
                "would have paired the note with an index of its old text:"
            )
            for note in stale:
                print(f"  {note.vault_path}")
            print(
                "\nRe-emit and re-author these entries. Re-running the same "
                "work file will now refuse them: the summaries were written "
                "against text the notes no longer have."
            )
            return 1
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill authored summaries onto vault notes that lack one.",
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write a work file for an agent to author summaries into.",
    )
    parser.add_argument(
        "--from",
        dest="work",
        type=Path,
        default=None,
        metavar="PATH",
        help="Read authored summaries from a filled-in work file.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Embed and write. Without it nothing is changed.",
    )
    arguments = parser.parse_args()

    if arguments.apply and arguments.work is None:
        parser.error("--apply needs --from <path>; there is nothing to write without it")
    if arguments.emit is not None and arguments.work is not None:
        parser.error("--emit plans a run and --from executes one; do one at a time")

    load_environment()

    # psycopg3's async pool drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. Matches
    # run_dev.py and the other scripts here. No-op on Linux/Heroku.
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            run(arguments.emit, arguments.work, arguments.apply),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(run(arguments.emit, arguments.work, arguments.apply))


if __name__ == "__main__":
    sys.exit(main())
