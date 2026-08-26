"""Pydantic models for the vault transport boundary.

Search and document retrieval over HTTP, plus the governed write path:
contribution (ADR 0016), full replacement (ADR 0018), the review queue
(ADR 0019's amendment), and wiki compilation. Export has models in neither this
module nor the router yet.

Persistence records and SQLAlchemy table definitions intentionally live in
separate modules.
"""

import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator

from .body_diff import MAX_BODY_DIFF_CHARS, BodyChangeSummary
from .domain import (
    AmendmentProposalKind,
    AmendmentProposalState,
    CompileWorkItem,
    DocumentKind,
    DocumentStatus,
    VaultAmendmentProposal,
    VaultCompileRun,
    VaultDocument,
    VaultReviewCase,
    VectorSearchStatus,
)
from .facets import normalize_facets


class VaultDocumentContentRequest(BaseModel):
    """Content fields and normalization shared by create and replacement."""

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
        """Transport-level shape and lossless normalization.

        Which facet *names* are legal is governance, checked in
        ``facets.validate_facets`` at the write boundary so that adding one
        stays a data change. Rejecting a scalar here rather than coercing it is
        deliberate: accepting both {"project": "hss"} and {"project": ["hss"]}
        would make every reader handle two shapes, and a containment query
        written for one silently misses the other.

        Normalization is safe only when it is one-to-one. Two distinct keys
        that both strip to ``project`` are rejected instead of allowing the
        later assignment to discard the earlier values.
        """

        for name, values in facets.items():
            if not isinstance(values, list):
                raise ValueError(
                    f"facet {name!r} must be a list of strings, not a bare value"
                )
            if any(not str(value).strip() for value in values):
                raise ValueError(f"facet {name!r} must not contain empty values")
        return normalize_facets(facets)


class VaultContributionRequest(VaultDocumentContentRequest):
    """Transport-neutral contribution input from the v1 tool contract."""

    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    origin: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Where this content came from before it reached the vault, for "
            "content with a life before this contribution -- "
            '{"author": "agent:codex", "created_at": "2026-07-30T18:54:39Z", '
            '"reference": "...", "run_id": "..."}. Leave it empty when the '
            "contributing credential is also the author, which is the ordinary "
            "case. It never affects where the note is stored, and the vault's "
            "own contributed_by and created_at are unaffected by it."
        ),
    )

    @field_validator("origin")
    @classmethod
    def validate_origin_shape(cls, origin: dict[str, str]) -> dict[str, str]:
        # Only the closed key set and the timestamp shape, both checked in the
        # service alongside facets so a contribution learns everything wrong
        # with it at once. Here we only refuse what the model layer owns.
        if any(not isinstance(value, str) for value in origin.values()):
            raise ValueError("origin values must be strings")
        return origin


class VaultDocumentUpdateRequest(VaultDocumentContentRequest):
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


class VaultReplacementChange(BaseModel):
    """A complete caller-controlled content replacement."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["replacement"]
    replacement: VaultDocumentUpdateRequest


class VaultBodyDiffChange(BaseModel):
    """A bounded unified diff against only the note body."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["body_diff"]
    body_diff: str = Field(min_length=1, max_length=MAX_BODY_DIFF_CHARS)


VaultAmendmentChange = Annotated[
    VaultReplacementChange | VaultBodyDiffChange,
    Field(discriminator="kind"),
]


class VaultAmendmentProposalRequest(BaseModel):
    """An immutable change proposed against one document revision."""

    model_config = ConfigDict(extra="forbid")

    target_note_id: str = Field(min_length=1, max_length=256)
    base_revision: int = Field(ge=1)
    change: VaultAmendmentChange
    rationale: str = Field(min_length=1, max_length=2_000)


class VaultAmendmentProposalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    target_note_id: str
    base_revision: int
    change_kind: AmendmentProposalKind
    state: AmendmentProposalState
    rationale: str
    proposed_by: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    applied_revision: int | None = None
    removals_acknowledged: bool = False


class VaultAmendmentProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: VaultAmendmentProposalSummary


class VaultAmendmentQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending: list[VaultAmendmentProposalSummary]
    count: int


class VaultAmendmentProposalDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: VaultAmendmentProposalSummary
    change: VaultAmendmentChange
    target: "VaultDocumentDetail | None"
    preview: "VaultAmendmentPreview | None"


class VaultRemovedBodyLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int = Field(ge=1)
    text: str


class VaultAmendmentPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resulting_body: str
    unified_diff: str
    added_line_count: int = Field(ge=0)
    removed_lines: list[VaultRemovedBodyLine]
    removed_line_count: int = Field(ge=0)
    hunk_count: int = Field(ge=0)
    requires_removal_acknowledgement: bool


class VaultAmendmentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accepted", "rejected"]
    decision_note: str | None = Field(default=None, max_length=2_000)
    acknowledge_removals: bool = False


class VaultAmendmentDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: VaultAmendmentProposalSummary
    outcome: Literal["accepted", "rejected", "stale"]
    target: "VaultDocumentDetail | None" = None


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
    similars: list[VaultSimilarNote] = Field(
        default_factory=list,
        description=(
            "Existing NOTES the candidate scored against. This is what the "
            "dedup gate judged, so a high score here is what 'flagged' means. "
            "`note_id` is a document id, resolvable with GET /notes/{id}."
        ),
    )
    related_pages: list[VaultSimilarNote] = Field(
        default_factory=list,
        description=(
            "Compiled WIKI PAGES near this note. CONTEXT, NOT A VERDICT: a "
            "page restates the notes it was built from, so resembling one is "
            "expected and is never why a contribution is flagged. Useful for "
            "deciding whether to extend an existing synthesis. Every entry is "
            "a wiki page by construction -- the query filters on kind -- and "
            "`note_id` carries its document id, the same id space every other "
            "surface uses and resolvable with GET /notes/{id}."
        ),
    )
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
    content_revision: int = Field(
        ge=1,
        description=(
            "Monotonic content version. Supply this as base_revision when "
            "proposing an amendment so newer edits cannot be overwritten."
        ),
    )


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


def document_detail(document: VaultDocument) -> VaultDocumentDetail:
    """Project a domain record onto the public read model.

    Deliberately a subset: the domain record carries persistence concerns the
    transport has no business publishing. Shared by both adapters for the same
    reason ``canonical_request_digest`` is -- two copies would let the HTTP and
    MCP read surfaces drift into disagreeing about what a note is.
    """

    return VaultDocumentDetail(
        note_id=document.id,
        kind=document.kind,
        doc_type=document.doc_type,
        vault_path=document.vault_path,
        status=document.status,
        doc_status=document.doc_status,
        title=document.title,
        summary=document.summary,
        body=document.body,
        tags=list(document.tags),
        aliases=list(document.aliases),
        facets={name: list(values) for name, values in document.facets.items()},
        related_ids=list(document.related_ids),
        source_ids=list(document.source_ids),
        source_url=document.source_url,
        created_at=document.created_at,
        updated_at=document.updated_at,
        content_revision=document.content_revision,
    )


def canonical_request_digest(body: VaultContributionRequest) -> bytes:
    """Hash a contribution request so a reused idempotency key can be checked.

    Hashes the validated model rather than the raw bytes: two JSON documents
    differing only in key order or whitespace are the same request, and
    treating them as a conflict would refuse a legitimate retry.

    Only the fields the caller actually supplied are covered. Serializing unset
    fields at their defaults made the digest a function of the *server's* schema
    as well as of the request, so adding an optional field silently changed the
    digest of every request that had ever been made -- see migration 0006 and
    ADR 0016's amendment. ``exclude_unset`` keeps the key-order and whitespace
    property above while making additive schema change a non-event.

    Any change to this function is a new ``service.REQUEST_DIGEST_VERSION``,
    because stored digests are not recomputable: the payloads that produced them
    were never kept.

    It lives here, beside the model it hashes, rather than in either adapter.
    Both the HTTP routes and the MCP tools must produce byte-identical digests
    for the same request -- a second copy in the second adapter is a silent
    idempotency bug waiting for the two to drift. It cannot live in ``service``
    next to ``REQUEST_DIGEST_VERSION``, its other half, because services take
    domain records and never Pydantic API models; that layer separation is the
    one this package guards most closely.
    """

    canonical = json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).digest()


class VaultSimilarEvidence(BaseModel):
    """One note a flagged contribution scored against.

    Shaped like ``VaultSimilarNote`` but read from stored JSON rather than a
    live query, so the fields are what the write path recorded at decision time
    and not what the corpus says now. A reviewer is judging the comparison that
    was actually made.
    """

    model_config = ConfigDict(extra="allow")

    note_id: str | None = None
    title: str | None = None
    score: float | None = None


class VaultReviewCaseSummary(BaseModel):
    """One case as it appears in the queue."""

    model_config = ConfigDict(extra="forbid")

    review_case_id: UUID
    # None once the candidate has been deleted by a rejection.
    candidate_note_id: str | None
    state: str
    reason: str
    similar: list[VaultSimilarEvidence] = Field(default_factory=list)
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


class VaultReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending: list[VaultReviewCaseSummary]
    count: int


class VaultReviewCaseResponse(BaseModel):
    """A case plus the content being judged.

    ``candidate`` is the flagged note in full. The public read surface withholds
    ``flagged`` (ADR 0008) because its consumer is a model that will not check
    the status field; a reviewer is the opposite consumer and cannot adjudicate
    what they cannot read.
    """

    model_config = ConfigDict(extra="forbid")

    review_case: VaultReviewCaseSummary
    candidate: VaultDocumentDetail | None


class VaultReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accepted", "rejected"] = Field(
        description=(
            "'accepted': the flag was a false positive, so the note is "
            "published and rejoins search and dedup. 'rejected': the note "
            "really is a duplicate, so it is DELETED -- its content is already "
            "in the corpus, which is what the case said. 'superseded' exists in "
            "the schema but is reserved and not accepted here."
        ),
    )
    decision_note: str | None = Field(default=None, max_length=2_000)


class VaultReviewDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_case: VaultReviewCaseSummary
    candidate: Literal["published", "deleted", "absent"] = Field(
        description=(
            "What happened to the note: 'published' if it is now active, "
            "'deleted' if it was removed, 'absent' if it was already gone."
        ),
    )


def review_case_summary(case: VaultReviewCase) -> VaultReviewCaseSummary:
    """Project a domain review case onto its transport shape.

    Here rather than in either adapter, for the reason ``document_detail`` is:
    two copies eventually disagree about what a case looks like.
    """

    return VaultReviewCaseSummary(
        review_case_id=case.id,
        candidate_note_id=case.candidate_document_id,
        state=case.state.value,
        reason=case.reason,
        similar=[VaultSimilarEvidence.model_validate(item) for item in case.similar_documents],
        created_at=case.created_at,
        decided_at=case.decided_at,
        decided_by=case.decided_by,
        decision_note=case.decision_note,
    )


def amendment_proposal_summary(
    proposal: VaultAmendmentProposal,
) -> VaultAmendmentProposalSummary:
    return VaultAmendmentProposalSummary(
        proposal_id=proposal.id,
        target_note_id=proposal.target_document_id,
        base_revision=proposal.target_revision,
        change_kind=proposal.change_kind,
        state=proposal.state,
        rationale=proposal.rationale,
        proposed_by=proposal.proposed_by,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        decided_by=proposal.decided_by,
        decision_note=proposal.decision_note,
        applied_revision=proposal.applied_revision,
        removals_acknowledged=proposal.removals_acknowledged,
    )


def amendment_proposal_change(
    proposal: VaultAmendmentProposal,
) -> VaultAmendmentChange:
    if proposal.change_kind is AmendmentProposalKind.BODY_DIFF:
        return VaultBodyDiffChange(kind="body_diff", **proposal.change)
    return VaultReplacementChange(
        kind="replacement",
        replacement=VaultDocumentUpdateRequest.model_validate(proposal.change),
    )


def amendment_preview(summary: BodyChangeSummary | None) -> VaultAmendmentPreview | None:
    if summary is None:
        return None
    removed = [
        VaultRemovedBodyLine(line_number=item.line_number, text=item.text)
        for item in summary.removed_lines
    ]
    return VaultAmendmentPreview(
        resulting_body=summary.resulting_body,
        unified_diff=summary.unified_diff,
        added_line_count=summary.added_line_count,
        removed_lines=removed,
        removed_line_count=len(removed),
        hunk_count=summary.hunk_count,
        requires_removal_acknowledgement=(
            summary.requires_removal_acknowledgement
        ),
    )


class VaultCompileWorkItem(BaseModel):
    """One page a run should write, and why.

    Note **ids**, never bodies. The compiling agent fetches what it needs
    through the ordinary read surface, which is already policy-checked (ADR
    0014); inlining bodies here would be a second read path with its own
    disclosure rules and a response the size of the corpus.
    """

    model_config = ConfigDict(extra="forbid")

    page_id: str | None = Field(
        default=None,
        description=(
            "The existing page to rewrite, or null for a page that does not "
            "exist yet. Pass it back as `page_id` when writing."
        ),
    )
    title: str | None = None
    reason: Literal["stale", "missing", "new-source"] = Field(
        description=(
            "'stale': a source moved after the page was compiled, or has since "
            "been flagged. 'missing': the page cites a note that no longer "
            "exists. 'new-source': a note no page covers at all."
        ),
    )
    source_ids: list[str]


class VaultCompileRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    state: Literal["running", "succeeded", "failed"]
    compiled_by: str
    started_at: datetime
    completed_at: datetime | None = None
    input_frontier: dict[str, Any] = Field(default_factory=dict)
    output_frontier: dict[str, Any] = Field(default_factory=dict)
    error_summary: str | None = None


class VaultCompilePlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: VaultCompileRunSummary
    items: list[VaultCompileWorkItem]
    count: int


class VaultCompilePageRequest(BaseModel):
    """One compiled page, written by the agent that synthesized it."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100_000)
    source_ids: list[str] = Field(
        min_length=1,
        description=(
            "The notes this page was synthesized from. Validated: unlike a "
            "note's related_ids, provenance naming something that does not "
            "exist is refused rather than stored."
        ),
    )
    summary: str | None = Field(default=None, max_length=2_000)
    tags: list[str] = Field(default_factory=list)
    related_ids: list[str] = Field(default_factory=list)
    page_id: str | None = Field(
        default=None,
        description=(
            "Rewrite this existing page rather than creating one. From the "
            "plan item's `page_id`."
        ),
    )


class VaultCompileSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_summary: str | None = Field(
        default=None,
        max_length=2_000,
        description=(
            "Required when failing a run; ignored when finishing one. A failed "
            "run keeps the pages it wrote and publishes no frontier, so the "
            "next plan re-covers what it did not finish."
        ),
    )


class VaultCompileDeclineRequest(BaseModel):
    """Notes this run considered and decided not to compile."""

    model_config = ConfigDict(extra="forbid")

    note_ids: list[str] = Field(
        min_length=1,
        max_length=500,
        description=(
            "Notes the run looked at and is refusing. Each is marked declined "
            "and stops appearing as a `new-source` work item -- until the note "
            "itself changes, which makes the decline stale and offers it again. "
            "Ids that resolve to no live note are refused rather than ignored."
        ),
    )


class VaultCompileDeclineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    declined_note_ids: list[str]
    declined_at: datetime = Field(
        description=(
            "When the judgement was recorded. A decline is compared against the "
            "note's own `updated_at`, so an edit after this instant re-offers it."
        ),
    )


def compile_run_summary(run: VaultCompileRun) -> VaultCompileRunSummary:
    """Project a domain compile run onto its transport shape.

    Here rather than in either adapter, for the reason ``document_detail`` is:
    two copies eventually disagree about what a run looks like.
    """

    return VaultCompileRunSummary(
        run_id=run.id,
        state=run.state.value,
        compiled_by=run.compiler_principal_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        input_frontier=run.input_frontier,
        output_frontier=run.output_frontier,
        error_summary=run.error_summary,
    )


def compile_work_item(item: CompileWorkItem) -> VaultCompileWorkItem:
    return VaultCompileWorkItem(
        page_id=item.page_id,
        title=item.title,
        reason=item.reason,  # type: ignore[arg-type]
        source_ids=list(item.source_ids),
    )
