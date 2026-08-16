"""Facet names and values: shape here, vocabulary in governance.

``vault_documents.facets`` classifies a note by things that are not topics --
which project it belongs to, which area, which system -- so that notes can be
related to each other without those relations entering the embedding text. See
ADR 0017 for why that exclusion is structural rather than a filter rule.

The split follows ADR 0009's precedent for ``doc_type``: the database CHECK
constrains **shape only** (an object of arrays of non-blank strings), and which
*names* are legal is enforced here, at the write boundary. Adding a facet stays
a data change rather than a migration.

``FACET_NAMES`` is deliberately a closed set while the values inside it are
open. A note may belong to any project without anyone declaring it first, but
inventing a whole new axis of classification is a governance decision -- an
unrecognised name is far more likely to be a typo (``projects``) that silently
files a note where nothing will look for it.

Nothing here does I/O.
"""

from .constants import MAX_FACET_NAME_LENGTH, MAX_FACET_VALUE_LENGTH


# The classification axes a contribution may use.
#
# Kept small on purpose. Each name is a question somebody will filter on, and a
# facet nobody filters by is a tag that costs a column read. Grow this when a
# consumer needs the axis, not in anticipation.
FACET_NAMES: frozenset[str] = frozenset(
    {
        # Which project the note is about. The operator's original request, and
        # already present in the corpus as the overloaded tags `hss` and
        # `b2-migration`.
        "project",
        # A durable area of responsibility rather than a bounded project.
        "area",
        # A named system or tool the note concerns.
        "system",
    }
)

# How many values one facet may carry, and how many facets one note may have.
# A note claiming twenty projects is a note that has not been split up; the
# limit exists so that stays visible rather than becoming a slow query.
MAX_VALUES_PER_FACET = 16
MAX_FACETS_PER_DOCUMENT = 8


class FacetNameCollision(ValueError):
    """Two distinct input names collapse to one normalized facet name."""


def normalize_facets(facets: dict[str, list[str]]) -> dict[str, list[str]]:
    """Strip, drop blanks, de-duplicate, and sort each facet's values.

    Sorted and de-duplicated for the same reason tags are in
    ``embedding_text``: reordering a list in frontmatter is not a change in
    meaning. Facets never reach the embedding, so this buys comparability and a
    stable projection rather than a saved embedding call.

    A facet whose values all normalize away is dropped entirely -- an empty
    array and an absent key would otherwise be two spellings of the same thing
    that a containment query treats differently.
    """

    normalized: dict[str, list[str]] = {}
    for name, values in facets.items():
        cleaned = sorted({value.strip() for value in values if value.strip()})
        if cleaned:
            normalized_name = name.strip()
            if normalized_name in normalized:
                raise FacetNameCollision(
                    f"facet names collide after normalization: {normalized_name!r}"
                )
            normalized[normalized_name] = cleaned
    return normalized


def validate_facets(facets: dict[str, list[str]]) -> list[str]:
    """Return human-readable errors (empty == valid).

    Mirrors ``governance.validate``: a list rather than an exception, so a
    contribution can report everything wrong with it at once instead of one
    problem per round trip.

    Runs against the *normalized* form, because that is what gets stored -- a
    facet that is legal only before normalization would fail confusingly.
    """

    errors: list[str] = []

    if len(facets) > MAX_FACETS_PER_DOCUMENT:
        errors.append(
            f"too many facets: {len(facets)} (limit {MAX_FACETS_PER_DOCUMENT})"
        )

    for name, values in facets.items():
        if name not in FACET_NAMES:
            known = ", ".join(sorted(FACET_NAMES))
            errors.append(f"unknown facet {name!r} (known facets: {known})")
        if len(name) > MAX_FACET_NAME_LENGTH:
            errors.append(
                f"facet name {name!r} exceeds {MAX_FACET_NAME_LENGTH} characters"
            )
        if len(values) > MAX_VALUES_PER_FACET:
            errors.append(
                f"facet {name!r} has {len(values)} values "
                f"(limit {MAX_VALUES_PER_FACET})"
            )
        for value in values:
            if len(value) > MAX_FACET_VALUE_LENGTH:
                errors.append(
                    f"facet {name!r} value {value[:32]!r}... exceeds "
                    f"{MAX_FACET_VALUE_LENGTH} characters"
                )

    return errors
