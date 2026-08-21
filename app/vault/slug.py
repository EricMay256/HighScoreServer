"""Title-derived file names for ``vault_path``.

A note's ``vault_path`` is its identity, its policy key (ADR 0010), and the name
of the file the export writes. A uuid satisfies the first two and fails the
third: a human browsing the projected vault sees a folder of hex, and no title
until they open one. ``reslug_vault.py`` exists in the Stage-A engine because
that was tried and abandoned there.

**Contributor input reaches the file name, and only the file name.** ADR 0022
originally stated flatly that no code path leads from contributor input to
``vault_path``; the 2026-08-20 amendment narrows that to what the privilege
argument actually needed. The concern was an agent *choosing its own folder* and
so choosing whether it landed in a tree it may not write. ``slugify`` cannot do
that: every run of non-alphanumeric characters collapses to a single hyphen, so
``/``, ``\\``, ``:`` and ``..`` cannot survive it, and the directory is supplied
by the service rather than derived from anything a caller sent. The invariant is
now "a contributor may influence the leaf name inside a service-chosen folder,
never the folder".

``slugify`` is a port of the Stage-A engine's ``store_git._slugify``, kept
diffable the way ``governance.py`` is kept diffable against ``vault_contrib.core``
(ADR 0004), so that both writers name a file for the same title identically.
"""

from collections.abc import Container


# The Stage-A engine's limit. Not a filesystem constraint -- it is well inside
# every relevant one -- but a readability one, and it has to match or the two
# writers disagree about a long title.
SLUG_MAX_LENGTH = 80

# Names Windows refuses for a file, with or without an extension. The vault runs
# on Linux and is developed on Windows, so the projection has to be writable in
# both places.
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
)


def slugify(title: str) -> str:
    """Filesystem-safe, human-readable slug from a title.

    Lowercase; any run of non-alphanumeric characters -- including the
    Windows-illegal set ``< > : " / \\ | ? *``, path separators, and whitespace
    -- collapses to one hyphen. ``str.isalnum`` is script-aware, so a non-ASCII
    title stays readable rather than becoming ``untitled``.

    Ported from vault_contrib.store_git._slugify.
    """

    out: list[str] = []
    previous_hyphen = False
    for character in title.strip().lower():
        if character.isalnum():
            out.append(character)
            previous_hyphen = False
        elif not previous_hyphen:
            out.append("-")
            previous_hyphen = True
    slug = "".join(out).strip("-")[:SLUG_MAX_LENGTH].strip("-")
    if not slug:
        return "untitled"
    if slug in WINDOWS_RESERVED_NAMES:
        return f"{slug}-note"
    return slug


def resolve_vault_path(directory: str, title: str, taken: Container[str]) -> str:
    """The first free ``<directory><slug>.md``, suffixing ``-2``, ``-3``, ... on collision.

    ``directory`` is supplied by the service and must already end in ``/``; it is
    never derived from caller input. ``taken`` holds the vault paths already in
    use under it.

    Two notes may legitimately share a title -- the dedup gate scores meaning,
    not titles -- so a collision is an ordinary event rather than an error, and
    ``vault_documents.vault_path`` is UNIQUE, so it must be resolved before the
    insert rather than caught after it. The caller resolves this under the
    corpus-wide advisory lock, which is what makes the answer still true by the
    time the row lands.
    """

    if not directory.endswith("/"):
        raise ValueError(f"directory must end in '/': {directory!r}")

    base = slugify(title)
    candidate = f"{directory}{base}.md"
    suffix = 2
    while candidate in taken:
        candidate = f"{directory}{base}-{suffix}.md"
        suffix += 1
    return candidate
