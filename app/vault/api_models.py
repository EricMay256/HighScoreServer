"""Pydantic models for the vault transport boundary.

The read-only slice exposes search and document retrieval over HTTP. The
contribution models below describe the write path, which is still unbuilt.
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
    tags: list[str] = Field(default_factory=list, max_length=50)
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


class VaultSimilarNote(BaseModel):
    """An existing note the deduper surfaced, as reported to a contributor."""

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    score: float


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
