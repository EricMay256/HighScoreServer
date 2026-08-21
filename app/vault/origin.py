"""Where a note's content came from before it was contributed here.

``vault_documents.origin`` answers a question the existing columns cannot. When
a corpus is replayed into the vault, the credential that transmits a note is not
the agent that wrote it, and the moment the row lands is not the moment the note
was authored. ``contributed_by`` and ``created_at`` are the *vault's* facts and
must stay that way -- ``contributed_by`` comes from the credential and never
from the body (ADR 0016), and backdating ``created_at`` would make the write
ledger disagree with itself. So the upstream facts get their own home.

**NULL-ish means "this vault is the origin".** An empty ``origin`` is the
ordinary case: an agent contributing now is both author and contributor, and the
row's own timestamps are the truth. Only content with a life before this vault
carries anything here.

One JSONB column rather than five, following ``facets``: the database CHECK
constrains **shape only** and the closed key set lives here, at the write
boundary, so a sixth provenance fact stays a data change rather than a migration
(ADR 0009's precedent). Unlike ``facets`` there is not even a containment query
to serve -- origin is bookkeeping that gets projected and never filtered on --
so the case for columns is weaker still.

**Timestamps are stored as the strings they arrived as.** They are ISO-8601
governance values, not instants the vault computes with; keeping the text means
the export re-emits exactly what the upstream note said, and no timezone
round-trip can move a value that nobody is doing arithmetic on. Shape is checked
here so a malformed one is a 422 rather than a surprise in a projected file.
"""

import re


# The provenance facts a contributor may state about content written elsewhere.
# Closed for the reason FACET_NAMES is: an unrecognised key is far more likely
# to be a typo that silently drops a fact than a new kind of provenance.
ORIGIN_KEYS: frozenset[str] = frozenset(
    {
        # The upstream note's `ContributedBy` -- `agent:<id>` or `human:<name>`.
        "author",
        # The upstream `CreatedAt` and `LastUpdated`, ISO-8601.
        "created_at",
        "updated_at",
        # The upstream `Source`: the Metadata Standard calls it "URL or run id",
        # so it is free text. `source_url` stays a validated URL and holds only
        # the cases that really are one.
        "reference",
        # The upstream `ClientRunID`: which authoring run produced the note.
        "run_id",
    }
)

# Keys whose value must parse as an ISO-8601 instant.
ORIGIN_TIMESTAMP_KEYS: frozenset[str] = frozenset({"created_at", "updated_at"})

MAX_ORIGIN_VALUE_LENGTH = 256

# Deliberately narrower than `datetime.fromisoformat`, which accepts a bare date
# and a naive datetime. A provenance timestamp with no offset is ambiguous by
# exactly the amount that made the exporter's UTC normalization necessary.
_ISO_8601 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def normalize_origin(origin: dict[str, str]) -> dict[str, str]:
    """Strip values and drop the ones that empty out.

    A key present with a blank value and a key absent are two spellings of "not
    stated", and keeping both would make every reader handle two. Mirrors
    ``normalize_facets``.
    """

    return {
        key: value.strip()
        for key, value in origin.items()
        if isinstance(value, str) and value.strip()
    }


def validate_origin(origin: dict[str, str]) -> list[str]:
    """Return human-readable errors (empty == valid).

    A list rather than an exception, matching ``validate_facets``: a
    contribution should learn everything wrong with it in one round trip.
    Runs against the normalized form, because that is what gets stored.
    """

    errors: list[str] = []

    for key, value in origin.items():
        if key not in ORIGIN_KEYS:
            known = ", ".join(sorted(ORIGIN_KEYS))
            errors.append(f"unknown origin field {key!r} (known fields: {known})")
            continue
        if len(value) > MAX_ORIGIN_VALUE_LENGTH:
            errors.append(
                f"origin {key!r} exceeds {MAX_ORIGIN_VALUE_LENGTH} characters"
            )
        if key in ORIGIN_TIMESTAMP_KEYS and not _ISO_8601.match(value):
            errors.append(
                f"origin {key!r} must be an ISO-8601 datetime with an offset, "
                f"got {value!r}"
            )

    return errors
