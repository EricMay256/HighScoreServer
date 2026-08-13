"""Pydantic models for the vault transport boundary.

Search and document retrieval over HTTP, plus the governed write path:
contribution (ADR 0016) and full replacement (ADR 0018). Review, compile, and
export have models in neither this module nor the router yet.

Persistence records and SQLAlchemy table definitions intentionally live in
separate modules.
"""

from datetime import datetime
from typing import Any

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator

from .domain import DocumentKind, DocumentStatus, VectorSearchStatus


class VaultContributionRequest(BaseModel):
    """Transport-neutral contribution input from the v1 tool contract."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    summary: str | None = Field(
        default=None,
        max_length=2_000,
        description=(
            "Optional short precis. Joins the embedding text and search_vector "
            "at weight B, so it is a semantic field rather than a display one."
        ),
    )
    tags: list[str] = Field(default_factory=list, max_length=50)
    aliases: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Alternative titles. Weighted 'A' in search alongside the title, "
            "because an alias is exactly the term a searcher types."
        ),
    )
    facets: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Classification relating this note to others, as {name: [values]} "
            "-- for example {\"project\": [\"highscoreserver\"]}. Deliberately "
            "NOT embedded: a shared value would pull every note carrying it "
            "together in the same vector space the dedup gate scores against. "
            "Use tags for topics and facets for belonging. See ADR 0017."
        ),
    )
    related_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Ids of notes this one relates to. Not checked for existence: a "
            "contribution may legitimately reference a note that is archived, "
            "flagged, or not yet written."
        ),
    )
    source_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Ids of notes this one was derived from.",
    )
    source_url: AnyUrl | None = None
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        if any(not tag for tag in tags):
            raise ValueError("tags must not contain empty values")
        if len(set(tags)) != len(tags):
            raise ValueError("tags must be unique")
        return tags

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, aliases: list[str]) -> list[str]:
        if any(not alias.strip() for alias in aliases):
            raise ValueError("aliases must not contain empty values")
        if len(set(aliases)) != len(aliases):
            raise ValueError("aliases must be unique")
        return aliases

    @field_validator("related_ids", "source_ids")
    @classmethod
    def validate_ids(cls, ids: list[str]) -> list[str]:
        if any(not value.strip() for value in ids):
            raise ValueError("ids must not contain empty values")
        if len(set(ids)) != len(ids):
            raise ValueError("ids must be unique")
        return ids

    @field_validator("facets")
    @classmethod
    def validate_facet_shape(cls, facets: dict[str, list[str]]) -> dict[str, list[str]]:
        """Transport-level shape only.

        Which facet *names* are legal is governance, checked in
        ``facets.validate_facets`` at the write boundary so that adding one
        stays a data change. Rejecting a scalar here rather than coercing it is
        deliberate: accepting both {"project": "hss"} and {"project": ["hss"]}
        would make every reader handle two shapes, and a containment query
        written for one silently misses the other.
        """

        for name, values in facets.items():
            if not isinstance(values, list):
                raise ValueError(
                    f"facet {name!r} must be a list of strings, not a bare value"
                )
            if any(not str(value).strip() for value in values):
                raise ValueError(f"facet {name!r} must not contain empty values")
        return facets


class VaultDocumentUpdateRequest(BaseModel):
    """Full replacement of one document's caller-supplied content.

    Deliberately a replacement rather than a patch. A patch would need a way to
    say "leave this alone" that is distinct from "set this to empty", and every
    optional field would carry that ambiguity; a replacement says what the
    document should now be, and an omitted list means an empty list. The cost is
    that a caller changing one facet resends the body, which is free for the
    only client that exists -- a projector that already holds the whole note.

    Carries no ``idempotency_key``. A full replacement is already idempotent:
    sending it twice converges, which is what PUT means. The contribution path
    needs a key because it mints identity and must not mint it twice.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    summary: str | None = Field(default=None, max_length=2_000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    facets: dict[str, list[str]] = Field(default_factory=dict)
    related_ids: list[str] = Field(default_factory=list, max_length=50)
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    source_url: AnyUrl | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        if any(not tag for tag in tags):
            raise ValueError("tags must not contain empty values")
        if len(set(tags)) != len(tags):
            raise ValueError("tags must be unique")
        return tags


class VaultSimilarNote(BaseModel):
    """An existing note the deduper surfaced, as reported to a contributor."""

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    score: float


class VaultDocumentUpdateResponse(BaseModel):
    """The settled outcome of one replacement."""

    model_config = ConfigDict(extra="forbid")

    note_id: str
    message: str
    re_embedded: bool = Field(
        description=(
            "Whether the edit changed the embedding text and therefore bought "
            "an embedding call. False means the change touched only fields the "
            "embedding does not read -- facets, related_ids, source_url -- so "
            "the stored vector was already correct."
        ),
    )
    similars: list[VaultSimilarNote] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class VaultContributionResponse(BaseModel):
    """Outcome of a governed write.

    Mirrors the `vault.contribute` output in the MCP tool schema. Note that
    `flagged` is a successful write, not a failure: the note landed and a
    review case was opened. Callers branch on `status`, so it is deliberately
    a small closed vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description=(
            "inserted | flagged | rejected | invalid. 'flagged' means the note "
            "was written and queued for adjudication, not that it failed."
        ),
    )
    note_id: str | None
    message: str
    idempotent_replay: bool = Field(
        default=False,
        description="True when this response replays an earlier identical request.",
    )
    similars: list[VaultSimilarNote] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class VaultDocumentResponse(BaseModel):
    """Deliberate public subset of a persisted vault document."""

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    body: str
    status: DocumentStatus
    tags: list[str]
    related_ids: list[str]
    provenance: dict[str, Any]
    canonical_url: AnyUrl
    created_at: datetime
    updated_at: datetime


class VaultDocumentDetail(BaseModel):
    """A document as returned by the read-only surface.

    Deliberately not ``VaultDocumentResponse``: that model carries
    ``canonical_url``, which needs a public base-URL setting the read-only slice
    does not define. Kept separate rather than making the field optional, so the
    write path's contract is not weakened to suit a different caller.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    kind: DocumentKind
    doc_type: str | None = Field(
        default=None,
        description=(
            "Governance Type Dictionary value, or null for an untyped "
            "document. Free text rather than an enum because the type "
            "vocabulary evolves without a schema change."
        ),
    )
    status: DocumentStatus
    doc_status: str | None = Field(
        default=None,
        description=(
            "Status Map value from types.yml (for example 'Evergreen' or "
            "'Stub'), or null. Distinct from `status`, which is the vault's "
            "own visibility state."
        ),
    )
    vault_path: str = Field(
        description=(
            "Vault-root-relative posix path of the source document, extension "
            "included."
        ),
    )
    title: str
    summary: str | None
    body: str
    tags: list[str]
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative titles, also indexed for lexical search.",
    )
    facets: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Classification relating this note to others, as {name: [values]}. "
            "Not part of the embedded text, so it never influences ranking -- "
            "it is what a consumer filters or groups by. See ADR 0017."
        ),
    )
    related_ids: list[str]
    source_ids: list[str]
    source_url: str | None
    created_at: datetime
    updated_at: datetime


class VaultSearchHit(VaultDocumentDetail):
    """A document plus why it surfaced for this query."""

    score: float = Field(description="Reciprocal rank fusion score.")
    lexical_rank: int | None = Field(
        default=None,
        description="1-based position in the full-text ranking, if it matched.",
    )
    vector_rank: int | None = Field(
        default=None,
        description="1-based position in the vector ranking, if it matched.",
    )


class VaultSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    profile_id: str | None = Field(
        default=None,
        description=(
            "Embedding profile the vector arm searched under, or null when no "
            "embedding provider is configured."
        ),
    )
    vector_status: VectorSearchStatus = Field(
        description=(
            "'used' when the vector arm contributed. 'not_configured' when no "
            "embedding provider is set up, which is a supported lexical-only "
            "deployment. 'failed' when a configured provider errored — these "
            "results are degraded and something is wrong."
        )
    )
    hits: list[VaultSearchHit]
