"""Pydantic models for the future vault transport boundary.

Phase 1 defines the stable value shapes but does not expose HTTP or MCP routes.
Persistence records and SQLAlchemy table definitions intentionally live in
separate modules.
"""

from datetime import datetime
from typing import Any

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator

from app.vault.domain import DocumentStatus


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
