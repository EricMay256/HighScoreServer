"""Project the service-authoritative ``Agent/`` tree back out as markdown.

ADR 0022 gives each tree exactly one writer: ``Human/`` is markdown-authoritative
and reaches the service by import, while ``Agent/`` is service-authoritative and
reaches a human by this module. A note that exists only in Postgres has no file
for a librarian to browse and none for wiki compilation to read; this is the
other direction of that pair.

**Byte-stable, or the git history stops being an audit log.** Every value comes
from the row -- never ``now()`` -- key order is fixed, timestamps are normalized
to seconds-precision UTC, and a rendered file identical to the one on disk is
not rewritten. Re-running over an unchanged corpus produces a zero-line diff.

The UTC normalization is load-bearing rather than cosmetic: ``timestamptz``
comes back in the *session's* time zone, so the same row renders differently on
two machines unless the exporter converts. That was observed on the real corpus,
not assumed.

**What is projected.** ``EXPORTED_PATH_PREFIXES`` mirrors the ``Agent/`` rules
that ``folders.yml`` marks ``engine_managed: true``. That deliberately excludes
``Agent/Promotion Candidates/``, which ``folders.yml`` marks
``engine_managed: false`` and the Promotion Policy calls "a human-curated queue,
**not** an engine-managed store ... kept outside the engine's dedup gate on
purpose". Writing files there would make this a second writer to a folder whose
whole point is human judgment. Nothing here touches ``Human/``: agents may not
write there at all, and the ``check-policy`` gate on ``ai/`` branches enforces
it.

**Flagged notes are exported.** ADR 0008 withholds ``flagged`` from *agents*,
because the consumer is a model that will not check the ``status`` field. A
librarian is the opposite consumer: a flagged note is precisely what needs
looking at. ``types.yml`` agrees -- ``Agent Note`` carries exactly the statuses
``Active`` and ``Flagged``, so a flagged note in ``Agent/notes/`` is a valid
note rather than an anomaly. For the same reason the projection does not apply
``READABLE_PATH_PREFIXES``: ``ai_read`` governs what agents are served, and a
human browsing their own vault is not that threat model (ADR 0022).

**The frontmatter renderer is a port**, kept diffable against the Stage-A
engine's ``vault_contrib.vault_frontmatter`` the way ``governance.py`` is kept
diffable against ``vault_contrib.core`` (ADR 0004). Only the writing half is
ported; nothing here parses markdown, so no YAML dependency is involved.

**Facets are projected as ``Facets: ["<name>/<value>", ...]``.** ADR 0017 makes
facets a load-bearing classification axis, and the Metadata Standard now carries
a universal ``Facets`` property for them. The flat encoding is deliberate: the
canonical renderer emits scalars and block sequences only, so a nested mapping
would need a second serializer and a second thing to keep byte-stable. See
``render_facets``.

**``SchemaVersion`` describes the file this module writes, so it comes from the
constant and not from the row.** ``constants.NOTE_SCHEMA_VERSION`` is the
frontmatter shape the projector emits, and the governance validator pins it --
2 for an Agent Note, 1 for a Wiki Page. ``vault_documents.schema_version``
records the shape a row was *created* under, which is the same number today and
need not stay so: a corpus migrated across a schema bump would carry the old
value on old rows while every projected file is written in the new shape.
Projecting the column would then describe the row rather than the file, and put
every un-migrated note into a validator warning.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import NOTE_SCHEMA_VERSION, WIKI_SCHEMA_VERSION
from .domain import DocumentKind, VaultDocument
from .repository import VaultDocumentRepository
from .service import VaultTransactionService


# Mirrors the `Agent/` rules in the private folders.yml that carry
# `engine_managed: true`. Not configuration, for the reason read_policy.py gives
# about READABLE_PATH_PREFIXES: which folders a machine may write is a
# governance decision, and a deployment must not be able to opt into projecting
# into one the governance layer reserved for a human.
EXPORTED_PATH_PREFIXES: tuple[str, ...] = (
    "Agent/notes/",
    "Agent/review/",
    "Agent/wiki/",
)

# Ported from vault_contrib.vault_frontmatter.SCHEMA_ORDER, with four keys the
# Stage-A note model has no field for and this one does: `aliases` and `Facets`,
# universal properties the Metadata Standard lists directly after `tags`;
# `Summary`, placed after `Title` to match the wiki order below; and
# `SourceIDs`, which global.yml lists as engine-owned plumbing. All four are
# omitted when empty, so an ordinary note renders in exactly Stage A's shape.
NOTE_KEY_ORDER: tuple[str, ...] = (
    "Type",
    "Status",
    "CreatedAt",
    "LastUpdated",
    "tags",
    "aliases",
    "Facets",
    "Title",
    "Summary",
    "ID",
    "ContributedBy",
    "Source",
    "RelatedIDs",
    "SourceIDs",
    "ClientRunID",
    "SchemaVersion",
)

# Ported from vault_contrib.vault_frontmatter.WIKI_SCHEMA_ORDER, plus `aliases`
# and `Facets` in the same positions as above.
WIKI_KEY_ORDER: tuple[str, ...] = (
    "Type",
    "Status",
    "CreatedAt",
    "LastUpdated",
    "tags",
    "aliases",
    "Facets",
    "Title",
    "Summary",
    "SourceIDs",
    "CompiledBy",
    "CompiledAt",
    "CompileRunID",
    "SchemaVersion",
    "Related",
)

# Keys rendered as YAML block sequences even when empty, so a list-typed
# property never renders as a scalar and never disappears.
LIST_KEYS: frozenset[str] = frozenset(
    {"tags", "aliases", "Facets", "RelatedIDs", "SourceIDs", "Related"}
)

_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "none", "~", ""}
_NUMERIC = re.compile(r"[+-]?(\d[\d_]*\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _needs_quote(value: str) -> bool:
    """Ported from vault_contrib.vault_frontmatter._needs_quote."""

    if value == "":
        return False
    if value.lower() in _RESERVED:
        return True
    if value != value.strip():
        return True
    if value[0] in "!&*?|>%@`\"'#,[]{}" or value[:2] in ("- ", "? ", ": "):
        return True
    if ": " in value or value.endswith(":") or " #" in value or '"' in value:
        return True
    if _NUMERIC.fullmatch(value):
        return True
    return False


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return _quote(text) if _needs_quote(text) else text


def _render_pair(key: str, value: Any) -> str:
    if key in LIST_KEYS or isinstance(value, (list, tuple)):
        items = list(value or [])
        if not items:
            return f"{key}: []"
        return "\n".join([f"{key}:"] + [f"  - {_scalar(item)}" for item in items])

    rendered = _scalar(value)
    return f"{key}:" if rendered == "" else f"{key}: {rendered}"


def render_frontmatter(
    metadata: Mapping[str, Any],
    order: Sequence[str],
) -> str:
    """Render metadata to canonical YAML, without the surrounding delimiters.

    Keys named in ``order`` come first in that order; anything else is appended
    alphabetically. The ordering is total and depends only on key names, which
    is what makes the output stable across runs and across processes.
    """

    keys = [key for key in order if key in metadata]
    keys += sorted(key for key in metadata if key not in order)
    return "\n".join(_render_pair(key, metadata[key]) for key in keys)


def dump_note(
    metadata: Mapping[str, Any],
    body: str,
    order: Sequence[str],
) -> str:
    """Serialize metadata and body into one markdown note."""

    frontmatter = render_frontmatter(metadata, order)
    body = body if body.endswith("\n") or body == "" else body + "\n"
    return f"---\n{frontmatter}\n---\n{body}"


def utc_timestamp(value: datetime) -> str:
    """Seconds-precision UTC with a ``Z`` suffix, the canonical engine shape.

    ``timestamptz`` values arrive in the database session's time zone, so this
    conversion is what makes two machines render one row identically.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Every key this module assigns from a column. A key of the same name inside the
# row's `frontmatter` JSONB describes the same fact and is dropped rather than
# allowed to contradict the column.
_ASSIGNED_KEYS: frozenset[str] = frozenset(NOTE_KEY_ORDER) | frozenset(WIKI_KEY_ORDER)


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    """One document's projection: where it goes and what it says."""

    document_id: str
    vault_path: str
    content: str
    # Fields carried by the row that the Metadata Standard has no property for,
    # so they are absent from `content`. Reported per run rather than silently
    # dropped.
    dropped_fields: tuple[str, ...] = ()
    # Governance problems visible from the row alone -- an untyped document, a
    # missing Status. The file is still written: refusing to project a row does
    # not fix it, and the report is what makes it findable.
    warnings: tuple[str, ...] = ()


def render_facets(facets: Mapping[str, Sequence[str]]) -> list[str]:
    """Flatten ``{"project": ["hss"]}`` to ``["project/hss"]``.

    One flat list rather than a nested mapping, because the canonical
    frontmatter renderer emits scalars and block sequences only -- a nested map
    would need a second serializer and a second thing to keep byte-stable. The
    separator is the first ``/``, which is unambiguous however many slashes a
    value contains: facet *names* are a closed set (``facets.FACET_NAMES``) and
    none of them contains one.

    Sorted, because ``normalize_facets`` already sorts values for the same
    reason -- reordering a classification is not a change in meaning, and an
    unstable order would rewrite the file on every run.
    """

    return sorted(
        f"{name}/{value}" for name, values in facets.items() for value in values
    )


def _common_frontmatter(document: VaultDocument) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "Type": document.doc_type,
        "Status": document.doc_status,
        "CreatedAt": utc_timestamp(document.created_at),
        "LastUpdated": utc_timestamp(document.updated_at),
        "tags": list(document.tags),
        "Title": document.title,
    }
    if document.aliases:
        metadata["aliases"] = list(document.aliases)
    if document.facets:
        metadata["Facets"] = render_facets(document.facets)
    if document.summary is not None:
        metadata["Summary"] = document.summary
    return metadata


# Which frontmatter key each `origin` field supplies, when the note carries one.
_ORIGIN_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("author", "ContributedBy"),
    ("created_at", "CreatedAt"),
    ("updated_at", "LastUpdated"),
    ("reference", "Source"),
    ("run_id", "ClientRunID"),
)


def _origin_overrides(document: VaultDocument) -> dict[str, Any]:
    """Upstream provenance, which wins over the column-derived value.

    The governance keys mean what the *note* says: `ContributedBy` is who wrote
    it and `CreatedAt` is when. For a replayed corpus those are the upstream
    facts, not the vault's -- `contributed_by` names the credential that
    transmitted the note (ADR 0016) and `created_at` is when the row landed.
    Projecting the vault's answers under the governance keys is exactly the
    lossiness the 2026-08-12 import produced.

    Nothing is hidden by this. Who transmitted and when is in the write ledger
    and the audit events, which outlive the document (ADR 0002) and are the
    right place for it.

    Timestamps pass through unparsed: they are stored as the ISO-8601 text the
    upstream note carried, so re-emitting them verbatim is both faithful and
    byte-stable. See origin.py.
    """

    return {
        key: document.origin[field_name]
        for field_name, key in _ORIGIN_KEY_MAP
        if field_name in document.origin
    }


def _note_frontmatter(document: VaultDocument) -> dict[str, Any]:
    metadata = _common_frontmatter(document)
    metadata.update(
        {
            "ID": document.id,
            "ContributedBy": document.contributed_by,
            "Source": document.source_url,
            "RelatedIDs": list(document.related_ids),
            # The idempotency key is not a column on the document: it lives in
            # `vault_write_requests`, keyed (principal_id, idempotency_key), and
            # a document accumulates one row per create and per update. There is
            # no single value to project, so the key renders empty rather than
            # inventing one. Nothing re-imports this tree -- that round trip is
            # what ADR 0022 exists to prevent -- so no consumer needs it.
            # From `origin` when the note had a life before this vault. The
            # vault's own idempotency key is not a substitute: it lives in
            # `vault_write_requests`, keyed (principal_id, idempotency_key),
            # and a document accumulates one row per create and per update, so
            # there is no single value to project.
            "ClientRunID": None,
            "SchemaVersion": NOTE_SCHEMA_VERSION,
        }
    )
    if document.source_ids:
        metadata["SourceIDs"] = list(document.source_ids)
    metadata.update(_origin_overrides(document))
    return metadata


def _wiki_frontmatter(document: VaultDocument) -> dict[str, Any]:
    metadata = _common_frontmatter(document)
    metadata.update(
        {
            "SourceIDs": list(document.source_ids),
            "CompiledBy": document.compiled_by,
            "CompiledAt": (
                utc_timestamp(document.compiled_at)
                if document.compiled_at is not None
                else None
            ),
            "CompileRunID": (
                str(document.compile_run_id)
                if document.compile_run_id is not None
                else None
            ),
            "SchemaVersion": WIKI_SCHEMA_VERSION,
            "Related": list(document.related_ids),
        }
    )
    return metadata


def _dropped_fields(document: VaultDocument) -> tuple[str, ...]:
    """Row fields with nowhere to go in the Metadata Standard.

    Empty today. It stays because the next column added to ``vault_documents``
    will land here before it lands in the standard, and a projection that
    silently omits a field is how a corpus loses one.
    """

    return ()


def _warnings(document: VaultDocument) -> tuple[str, ...]:
    warnings: list[str] = []
    if not document.doc_type:
        warnings.append("doc_type is NULL, so the note carries no Type")
    if not document.doc_status:
        warnings.append("doc_status is NULL, so the note carries no Status")
    return tuple(warnings)


def render_document(document: VaultDocument) -> RenderedDocument:
    """Render one row as the markdown note it projects to."""

    if document.kind is DocumentKind.WIKI:
        metadata = _wiki_frontmatter(document)
        order: Sequence[str] = WIKI_KEY_ORDER
    else:
        metadata = _note_frontmatter(document)
        order = NOTE_KEY_ORDER

    # Frontmatter the schema does not model, kept so the projection can re-emit
    # what the note said. A key colliding with a column-derived one loses: the
    # column is the row's current value, the JSONB copy is whatever an import
    # once found.
    for key, value in sorted(document.frontmatter.items()):
        if key not in _ASSIGNED_KEYS:
            metadata[key] = value

    return RenderedDocument(
        document_id=document.id,
        vault_path=document.vault_path,
        content=dump_note(metadata, document.body, order),
        dropped_fields=_dropped_fields(document),
        warnings=_warnings(document),
    )


class ExportPathError(ValueError):
    """A row's ``vault_path`` cannot be turned into a file safely."""


def resolve_export_path(root: Path, vault_path: str) -> Path:
    """Map a vault-relative path to a file under ``root``, or refuse.

    The database CHECK already forbids absolute paths, ``..`` segments,
    backslashes, and trailing slashes. This is the second layer, applied where
    a string becomes a filesystem write: an export that escapes its output
    directory is the one failure re-running cannot undo.
    """

    if not any(vault_path.startswith(prefix) for prefix in EXPORTED_PATH_PREFIXES):
        raise ExportPathError(
            f"{vault_path!r} is outside the exported prefixes "
            f"{EXPORTED_PATH_PREFIXES}"
        )
    pure = PurePosixPath(vault_path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ExportPathError(f"{vault_path!r} is not a safe relative path")

    resolved_root = root.resolve()
    target = (resolved_root / Path(*pure.parts)).resolve()
    if resolved_root not in target.parents:
        raise ExportPathError(f"{vault_path!r} resolves outside {resolved_root}")
    return target


@dataclass(slots=True)
class ExportReport:
    """What one run saw and did.

    Counts, except where a human has to act: warnings and prunable files are
    named individually, because "3 files would be deleted" is not something
    anyone can check.
    """

    scanned: int = 0
    written: int = 0
    unchanged: int = 0
    pruned: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    prunable: list[str] = field(default_factory=list)

    def note_dropped(self, fields: Iterable[str]) -> None:
        for name in fields:
            self.dropped[name] = self.dropped.get(name, 0) + 1


class VaultExportService:
    """Write the service-authoritative ``Agent/`` tree to a directory.

    The service owns the transaction while the repository stays
    connection-injected (ADR 0001). File I/O lives here rather than in a script
    so the script contains no SQL and no schema knowledge.
    """

    def __init__(
        self,
        transactions: VaultTransactionService,
        documents: VaultDocumentRepository | None = None,
        page_size: int = 200,
    ) -> None:
        self._transactions = transactions
        self._documents = documents or VaultDocumentRepository()
        self._page_size = page_size

    async def rendered_documents(self) -> tuple[RenderedDocument, ...]:
        """Every exportable row, rendered, ordered by ``vault_path``.

        Paged inside one transaction so the walk sees one consistent corpus. A
        note created midway through must not appear on a later page while being
        absent from the prune set computed from the same run.
        """

        rendered: list[RenderedDocument] = []
        async with self._transactions.transaction() as connection:
            cursor: str | None = None
            while True:
                page = await self._documents.list_under_path_prefixes(
                    connection,
                    EXPORTED_PATH_PREFIXES,
                    after_vault_path=cursor,
                    limit=self._page_size,
                )
                if not page:
                    break
                rendered.extend(render_document(document) for document in page)
                cursor = page[-1].vault_path
        return tuple(rendered)

    async def export(
        self,
        root: Path,
        apply: bool = False,
        prune: bool = False,
    ) -> ExportReport:
        """Project the corpus into ``root``.

        ``apply`` is off by default: this writes into a tree a human curates,
        and every script in this repository that touches a live target asks to
        be told twice.
        """

        report = ExportReport()
        rendered = await self.rendered_documents()
        expected: set[Path] = set()

        for item in rendered:
            report.scanned += 1
            target = resolve_export_path(root, item.vault_path)
            expected.add(target)
            report.note_dropped(item.dropped_fields)
            report.warnings.extend(
                f"{item.vault_path}: {warning}" for warning in item.warnings
            )

            if target.exists() and _read_text(target) == item.content:
                report.unchanged += 1
                continue
            report.written += 1
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_text(target, item.content)

        resolved_root = root.resolve()
        # Only sweep a prefix the corpus actually populates. A prefix with no
        # rows is far more likely to mean "the database is not authoritative
        # for this folder yet" than "every file in it was retired" -- and today
        # it means exactly that for `Agent/wiki/`, where the Stage-A librarian
        # still owns 15 compiled pages the service has never held. ADR 0012
        # settles the same question the same way for reconciliation: sweep only
        # after a complete walk, and refuse an implausible one.
        populated = {
            prefix
            for prefix in EXPORTED_PATH_PREFIXES
            if any(item.vault_path.startswith(prefix) for item in rendered)
        }
        for orphan in _orphaned_files(root, expected, populated):
            report.prunable.append(orphan.relative_to(resolved_root).as_posix())
            if prune and apply:
                orphan.unlink()
                report.pruned += 1

        return report


def _read_text(path: Path) -> str:
    # newline="" so an existing CRLF file compares unequal to LF output instead
    # of being normalized on read and therefore never rewritten.
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_text(path: Path, content: str) -> None:
    # newline="" disables translation, so the file carries exactly the rendered
    # bytes -- LF -- on Windows as well as on Linux.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _orphaned_files(
    root: Path,
    expected: set[Path],
    prefixes: Iterable[str],
) -> tuple[Path, ...]:
    """Markdown files under the given prefixes that no row accounts for.

    Scoped to the prefixes this module owns, so a file the engine never wrote --
    ``Agent/INDEX.md``, anything under ``Agent/Promotion Candidates/`` -- is
    never a deletion candidate, whatever the corpus contains. Narrowed again by
    the caller to prefixes the corpus populates, so an empty one is left alone
    rather than emptied.
    """

    resolved_root = root.resolve()
    orphans: list[Path] = []
    for prefix in prefixes:
        directory = resolved_root / Path(*PurePosixPath(prefix).parts)
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.resolve() not in expected:
                orphans.append(path)
    return tuple(orphans)
