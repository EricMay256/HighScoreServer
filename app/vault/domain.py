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
    summary: str | None = None
    tags: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    embedding_model: str | None = None
    embedded_at: datetime | None = None
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
    summary: str | None = None
    tags: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    embedded_at: datetime | None = None
    compile_run_id: UUID | None = None
    compiled_by: str | None = None
    compiled_at: datetime | None = None


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
