"""Domain records for the vault bounded context."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID


class DocumentKind(str, Enum):
    NOTE = "note"
    WIKI = "wiki"


class DocumentStatus(str, Enum):
    ACTIVE = "active"
    FLAGGED = "flagged"
    ARCHIVED = "archived"


class ReviewState(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class WriteRequestState(str, Enum):
    PROCESSING = "processing"
    INSERTED = "inserted"
    FLAGGED = "flagged"
    INVALID = "invalid"
    FAILED = "failed"


class CompileRunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VectorSearchStatus(str, Enum):
    """Whether the vector arm contributed to a search, and if not, why.

    A plain "did it run" boolean conflates two very different situations: a
    deployment that never configured embeddings, which is a supported mode, and
    a configured provider that just failed, which is a fault someone needs to
    see. Callers get lexical results either way, so the distinction has to be
    carried in the response rather than inferred from a shorter result list.
    """

    USED = "used"
    # No provider configured for this process. Expected in CI and local runs.
    NOT_CONFIGURED = "not_configured"
    # A provider is configured and the query embedding failed. Degraded, and
    # not on purpose.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class VaultDocument:
    id: str
    kind: DocumentKind
    status: DocumentStatus
    title: str
    body: str
    contributed_by: str
    provenance: dict[str, Any]
    schema_version: int
    created_at: datetime
    updated_at: datetime
    # Governance Type Dictionary value, validated against types.yml at the
    # write boundary rather than here. None means untyped, which is a real
    # state rather than missing data. See ADR 0009.
    doc_type: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    compile_run_id: UUID | None = None
    compiled_by: str | None = None
    compiled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewVaultDocument:
    id: str
    kind: DocumentKind
    status: DocumentStatus
    title: str
    body: str
    contributed_by: str
    provenance: dict[str, Any]
    schema_version: int = 1
    doc_type: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    compile_run_id: UUID | None = None
    compiled_by: str | None = None
    compiled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocumentEmbedding:
    """One document's vector under one embedding profile.

    A document with no row for a profile is simply not embedded under it, which
    is why there is no "pending" state here.
    """

    document_id: str
    profile_id: str
    vector: tuple[float, ...]
    # Unset on the way in so the column default applies; populated on read.
    embedded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VaultReviewCase:
    id: UUID
    candidate_document_id: str
    state: ReviewState
    reason: str
    similar_documents: tuple[dict[str, Any], ...]
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    pool_size: int
    checked_out: int
    checkout_count: int
    checkin_count: int
    checkout_failures: int
    latest_checkout_seconds: float | None
    maximum_checkout_seconds: float | None
    total_checkout_seconds: float = field(repr=False)
