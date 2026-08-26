"""Translate between the two edge vocabularies at the corpus boundaries.

ADR 0025 settles that **inside the database every edge is an id**, while a human
writing markdown still writes ``[[Some Note]]`` and never sees one. Neither
vocabulary wins; they are translated at the two boundaries that already exist,
and this module is the translation both of them use.

**Inbound (import, repair): a name becomes an id, or it is dropped.** An
unresolved name is not an id, and ``related_ids`` holds ids. Resolution happens
once, converting a fragile *name* reference into a stable *id* reference.

**Outbound (export): an id becomes ``[[slug]]``.** The exporter holds every
id-to-slug pair for the run before it writes anything, so an id that does not
resolve within the run is omitted rather than rendered -- a broken wikilink is
worse than an absent one, and dangling edges are legal (ADR 0025).

**A name resolves by title, alias, or slug, and ambiguity is never guessed.**
Two documents may legitimately share a title -- the dedup gate scores meaning,
not titles -- so a name denoting more than one row is reported to a human rather
than resolved to whichever row sorted first. ``candidates`` returns every id a
name could mean and lets the caller decide; the two callers in this repository
both refuse to write an ambiguous edge.

Matching is two-tier and the tiers are ordered. An exact match on a title,
alias, or slug (case-folded) wins outright. Only when that finds nothing does
the loose tier run, which compares both sides through :func:`slug.slugify`, so
``[[JSONB Facet Columns: Constraints and Indexes]]`` still reaches the page
whose file is ``jsonb-facet-columns-constraints-and-indexes.md``. The order
matters: the loose tier deliberately erases punctuation, so letting it run first
would let a punctuation collision beat an exact title.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .domain import VaultDocument
from .slug import slugify


# One whole value that is a wikilink and nothing else. Deliberately anchored:
# this reads *stored column values*, where the only legal shapes are an id and a
# link somebody's import left behind. Finding links inside prose is a different
# job with a different regex, and doing both here would let a body mention turn
# into an edge.
#
# Obsidian's target syntax is `[[target#heading|display]]`, both suffixes
# optional. The target is everything before the first `#` or `|`.
_WIKILINK = re.compile(r"^\[\[(?P<target>[^\[\]]+)\]\]$")


def parse_wikilink(value: str) -> str | None:
    """The link target inside ``[[...]]``, or None when the value is not one.

    None is the answer for an ordinary document id, which is what makes this
    safe to run over a whole column: a value that is already an id passes
    through untouched.
    """

    match = _WIKILINK.fullmatch(value.strip())
    if match is None:
        return None
    target = match.group("target").split("|", 1)[0].split("#", 1)[0].strip()
    return target or None


def render_wikilink(slug: str) -> str:
    """A slug as the link an Obsidian reader can follow."""

    return f"[[{slug}]]"


# Characters no document id contains and every written *name* eventually does:
# the brackets of a wikilink, and the spaces of a title. Both `vault_documents.id`
# and its callers agree on this much without agreeing on a format -- the service
# mints `uuid4().hex` and the corpus has carried Stage-A hex ids, and neither has
# ever held whitespace or a bracket.
_NOT_IN_ANY_ID = re.compile(r"[\s\[\]]")


def looks_like_a_name(value: str) -> bool:
    """True when an edge value is plainly a name rather than a document id.

    **Deliberately weaker than "is this a valid id".** ADR 0030 chose a rule
    that recognises the mistake without pinning the id *format* into the wire
    contract: a validator spelling `^[0-9a-f]{32}$` would be exactly right today
    and would have to be revised, on a shipped API, the first time an id gained
    a prefix or became a ULID.

    The residual is real and accepted: a bare slug (``operating-the-agent-vault``)
    contains neither whitespace nor a bracket, so it passes this and is stored
    like any other unresolvable id. Catching that means knowing the format, which
    is the trade ADR 0030 declines.
    """

    return _NOT_IN_ANY_ID.search(value) is not None


def slug_of(vault_path: str) -> str:
    """The leaf name a wikilink to this document would use.

    ``vault_path``'s leaf is the title's slug (ADR 0022's 2026-08-20 amendment),
    and Obsidian resolves ``[[x]]`` against a *file name* -- so the slug, not the
    title, is what a link has to carry.
    """

    return PurePosixPath(vault_path).stem


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """One document as something a wikilink can name.

    Four fields rather than a ``VaultDocument`` because the callers that build
    an index are a script running a four-column SELECT and an import holding
    rows that do not exist yet. Neither has a document, and requiring one would
    mean loading every body to resolve a name.
    """

    document_id: str
    title: str
    slug: str
    aliases: tuple[str, ...] = ()


class LinkIndex:
    """Every id-to-slug pair for one corpus, and the name lookups back.

    Built once per run and then read, which is the shape ADR 0025 requires of
    the export: hold every pair *before* writing anything, so "does this id
    resolve within the run" has a stable answer for the whole run.
    """

    def __init__(self, targets: Iterable[LinkTarget]) -> None:
        self._slugs: dict[str, str] = {}
        self._exact: dict[str, set[str]] = {}
        self._loose: dict[str, set[str]] = {}
        for target in targets:
            self._slugs[target.document_id] = target.slug
            for name in (target.title, target.slug, *target.aliases):
                if not name.strip():
                    continue
                self._exact.setdefault(name.strip().casefold(), set()).add(
                    target.document_id
                )
                self._loose.setdefault(slugify(name), set()).add(target.document_id)

    @classmethod
    def from_documents(cls, documents: Iterable[VaultDocument]) -> "LinkIndex":
        return cls(
            LinkTarget(
                document_id=document.id,
                title=document.title,
                slug=slug_of(document.vault_path),
                aliases=tuple(document.aliases),
            )
            for document in documents
        )

    def slug_for(self, document_id: str) -> str | None:
        """The slug to link to, or None when this id is not in the run."""

        return self._slugs.get(document_id)

    def candidates(self, name: str) -> tuple[str, ...]:
        """Every id the name could denote: none, one, or several.

        Several is not an error here and not a coin toss either -- it is handed
        back for a human to settle, because the two things a shared title can
        mean (a genuine duplicate, or two notes that happen to be named alike)
        need different fixes.
        """

        exact = self._exact.get(name.strip().casefold())
        if exact:
            return tuple(sorted(exact))
        return tuple(sorted(self._loose.get(slugify(name), ())))


@dataclass(frozen=True, slots=True)
class EdgeResolution:
    """What one stored edge list became, and what that cost.

    ``values`` is the column as it should be written. The other three fields
    exist so a caller can report rather than only act: dropping an edge silently
    destroys the only evidence the note was ever cited, which is the mistake
    ``remap_vault_reference_ids`` is careful not to make either.
    """

    values: tuple[str, ...]
    # (wikilink, id) for each name that resolved.
    resolved: tuple[tuple[str, str], ...] = ()
    # Names that resolved to nothing. Dropped from `values`, per ADR 0025.
    dropped: tuple[str, ...] = ()
    # (wikilink, candidate ids) for each name denoting more than one document.
    # Left in `values` exactly as they were: this module does not guess.
    ambiguous: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.resolved or self.dropped)


def resolve_edges(values: Sequence[str], index: LinkIndex) -> EdgeResolution:
    """Turn any wikilinks in a stored edge list into the ids they name.

    Values that are already ids pass through untouched and in place, so a second
    run over a repaired column writes nothing.

    Order is preserved. Duplicates are not: two names resolving to one document
    would leave the same edge twice, which is meaningless as a relation and
    would fail ``VaultContentRequest``'s uniqueness rule the next time anything
    updated the note through the API. Only duplicates *resolution created* are
    collapsed -- a value that arrives twice unchanged stays twice, because
    tidying an edge list is not this function's job.
    """

    out: list[str] = []
    resolved: list[tuple[str, str]] = []
    dropped: list[str] = []
    ambiguous: list[tuple[str, tuple[str, ...]]] = []
    minted: set[str] = set()

    for value in values:
        name = parse_wikilink(value)
        if name is None:
            out.append(value)
            continue
        candidates = index.candidates(name)
        if len(candidates) == 1:
            document_id = candidates[0]
            resolved.append((value, document_id))
            if document_id in minted:
                continue
            minted.add(document_id)
            out.append(document_id)
        elif candidates:
            ambiguous.append((value, candidates))
            out.append(value)
        else:
            dropped.append(value)

    return EdgeResolution(
        values=tuple(out),
        resolved=tuple(resolved),
        dropped=tuple(dropped),
        ambiguous=tuple(ambiguous),
    )


def render_edges(document_ids: Iterable[str], index: LinkIndex) -> list[str]:
    """Ids as ``[[slug]]``, omitting every id the run cannot resolve.

    The omission is ADR 0025's rule and not a convenience: an id pointing at a
    note outside the exported prefixes, or at one that no longer exists, would
    render as a wikilink Obsidian shows as broken. An absent link is a graph
    with a gap; a broken one is a graph that lies about having an edge.
    """

    slugs: list[str] = []
    for document_id in document_ids:
        slug = index.slug_for(document_id)
        if slug is not None:
            slugs.append(render_wikilink(slug))
    return slugs
