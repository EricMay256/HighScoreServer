"""Pydantic models for the vault transport boundary.

Search and document retrieval over HTTP, plus the governed write path:
contribution (ADR 0016), full replacement (ADR 0018), the review queue
(ADR 0019's amendment), and wiki compilation. Export has models in neither this
module nor the router yet.

Persistence records and SQLAlchemy table definitions intentionally live in
separate modules.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .body_diff import MAX_BODY_DIFF_CHARS, BodyChangeSummary
from .constants import SUMMARY_GRACE_PERIOD_SECONDS
from .domain import (
    AmendmentProposalKind,
    AmendmentProposalState,
    CompileWorkItem,
    DocumentKind,
    DocumentStatus,
    EdgeRef,
    MetadataChangeSummary,
    VaultAmendmentProposal,
    VaultCompileRun,
    VaultDocument,
    VaultDocumentBrief,
    VaultReviewCase,
    VectorSearchStatus,
)
from .facets import normalize_facets
from .search import SearchResult
from .snippet import lead_snippet
from .wikilinks import looks_like_a_name, slug_of


# The transport bounds the write path accepts, named because more than one
# adapter has to publish the same numbers. The MCP span-edit tool declares them
# on its parameters so a generated client can discover them; leaving it to a
# hand-written check meant the schema said `string` and the bound existed only
# at runtime, which is a bound no caller can see.
MAX_BODY_CHARS = 100_000
MAX_RATIONALE_CHARS = 2_000
MAX_DOCUMENT_ID_CHARS = 256
# The cap on edges one *note* may declare. Distinct from
# MAX_EDGE_LOOKUP_IDS below, which bounds one lookup request -- they were
# both called MAX_EDGE_IDS in different modules, with different values.
MAX_EDGE_IDS = 50

# The cap on ids one edge-resolution request may name. Larger than the
# per-note cap because a compiled page may cite more sources than a note
# may declare edges, and the console batches to this size.
MAX_EDGE_LOOKUP_IDS = 100

# The cap on each edge list of a compiled page. Chosen from the other end: the
# browse console resolves the *union* of `source_ids` and `related_ids`, in
# batches of MAX_EDGE_LOOKUP_IDS, and stops after ten of them to leave the
# reader quota for their next click. Two lists of this size are exactly that
# thousand, so anything the server accepts, the console can resolve.
#
# Left unbounded, ids past the console's thousandth were unreachable forever:
# a retry restarts at the first id, so the tail was never requested, while the
# page said "retry to resolve them". A number nobody can reach beats a promise
# nobody can keep. It is generous against the corpus it governs -- notes cap
# their own edges at 50, and a page citing 500 sources would have to cite five
# times every document that exists.
MAX_PAGE_EDGE_IDS = 500
MAX_AMENDMENT_BATCH_DECISIONS = 50


def validate_edge_ids(ids: list[str]) -> list[str]:
    """The rule every edge list obeys, wherever it enters the system.

    Module-level rather than a method because more than one model writes edges
    now. The metadata change kind shipped without this and accepted duplicates
    and `[[wikilinks]]` -- reintroducing, on a brand-new path, exactly the
    corruption ADR 0030 was written to stop and that
    `scripts/resolve_vault_wikilinks` exists to repair. A second edge-writing
    surface needs the same validator, not a similar one.
    """

    if any(not value.strip() for value in ids):
        raise ValueError("ids must not contain empty values")
    if len(set(ids)) != len(ids):
        raise ValueError("ids must be unique")
    # Shape, never existence. ADR 0025 keeps an edge unvalidated on purpose
    # -- a contribution may reference a note that is archived, flagged, or
    # not yet written -- and ADR 0030 draws the line that decision does not:
    # a value carrying a bracket or a space is not an id that points at
    # nothing, it is a name, and `related_ids` holds ids. Twenty-one of them
    # reached production because nothing said so.
    named = [value for value in ids if looks_like_a_name(value)]
    if named:
        raise ValueError(
            "ids must be document ids, not titles or wikilinks: "
            f"{', '.join(repr(value) for value in named[:3])}"
        )
    return ids


def validate_source_url_intent(
    source_url: AnyUrl | None, clear_source_url: bool
) -> None:
    """A request may set the URL or clear it, never both.

    `clear_source_url` exists because a null `source_url` cannot mean both
    "leave it alone" and "remove it" over JSON. Carrying both a replacement URL
    and the clear flag reintroduces exactly the ambiguity the flag was added to
    remove, and the service resolved it by precedence: the flag won, the URL
    the caller supplied was dropped, and a request to *change* a value silently
    became a request to delete one.

    Refused rather than resolved. There is no reading of the pair that is
    obviously right, so guessing turns a caller's mistake into data loss they
    are never told about, on the one path in this kind that can destroy
    something.
    """

    if clear_source_url and source_url is not None:
        raise ValueError(
            "source_url and clear_source_url=true are contradictory: send "
            "source_url to replace the existing URL, or clear_source_url=true "
            "to remove it, not both"
        )


class VaultAuthorizationResponse(BaseModel):
    """What the credential on this request is, in the terms an operator uses.

    Everything here is already known to whoever presented the token, which is
    why the endpoint requires no scope: it discloses nothing a caller could not
    derive from the token string and a 403. What it adds is the ``label``,
    which lives on the authorization rather than in the token and so cannot be
    read out of it.

    ``label`` is unverified operator text and is display only -- ADR 0040. It
    is not an identifier: two authorizations may share one, and nothing is
    resolved by it. ``credential_id`` and ``principal_id`` remain the identity.
    """

    model_config = ConfigDict(extra="forbid")

    credential_id: str
    principal_id: str
    scopes: list[str]
    label: str | None = None


class VaultDocumentContentRequest(BaseModel):
    """Content fields and normalization shared by create and replacement."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
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
            "flagged, or not yet written. They must still be ids -- a title or "
            "a [[wikilink]] is rejected, because the graph is stored as ids and "
            "rendered as links only on export."
        ),
    )
    source_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Ids of notes this one was derived from. Same rule as "
            "`related_ids` and enforced by the same validator: not checked for "
            "existence, but a title or a [[wikilink]] is rejected."
        ),
    )
    source_url: AnyUrl | None = None

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, summary: str | None) -> str | None:
        """One rule for what an absent summary is, shared by every write path.

        A blank or whitespace-only summary is stored as ``None`` rather than
        kept verbatim. Keeping it produced a note that was non-null and
        meaningless: it suppressed `summary_advice` because the adapters test
        `summary is not None`, it could not be repaired by `vault_set_summary`
        because that carveout requires the column to be null, and the backfill
        skipped it for the same reason. The note was permanently undescribed
        with nothing able to notice.

        Normalizing rather than rejecting because `summary=""` means the same
        thing the caller would have said by omitting the field, and refusing it
        would break a caller for stating it a different way. Real summaries are
        stripped, since trailing whitespace is not content.
        """

        if summary is None:
            return None
        stripped = summary.strip()
        return stripped or None

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
        return validate_edge_ids(ids)

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


class VaultSetSummaryRequest(BaseModel):
    """Supply the ``summary`` a contribution omitted (ADR 0035).

    One field, and that is the whole contract. This is not a narrow update: it
    is a distinct operation that can only ever move ``summary`` from absent to
    present, on a note the caller contributed, inside the grace period. Every
    other content field is unreachable from here by construction rather than by
    validation, which is what lets it sit under ``vault:write`` while a general
    replacement stays behind ``vault:update``.

    The bound matches ``VaultDocumentContentRequest.summary`` because it is the
    same column; a value accepted at contribute time must be accepted here, or
    the carveout would refuse to repair exactly what it exists to repair.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        min_length=1,
        max_length=2_000,
        description=(
            "Short precis of the whole note. Joins the embedding text and "
            "search_vector at weight B and becomes the note's search preview, "
            "so it is a retrieval signal rather than a display field. Write "
            "what the note concludes, not what it is filed under."
        ),
    )

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, summary: str) -> str:
        """Reject a blank summary, and store the stripped value.

        ``min_length`` alone would admit whitespace, and whitespace is the one
        input this operation must not accept: it writes a non-null column, so
        it would close the carveout permanently -- the note can never be
        described again through this route -- while contributing nothing to
        either the embedding text or the preview. A refusal the caller can act
        on is strictly better than a write it cannot undo.

        Scoped to this model deliberately. ``VaultDocumentContentRequest``
        accepts a blank summary today, and tightening a shipped write path is a
        separate decision from how a new one behaves.
        """

        stripped = summary.strip()
        if not stripped:
            raise ValueError("summary must not be blank")
        return stripped


class VaultSetSummaryResponse(BaseModel):
    """The settled outcome of filling in an absent summary."""

    model_config = ConfigDict(extra="forbid")

    note_id: str
    message: str
    content_revision: int = Field(
        description=(
            "The note's content revision after the write. It moves because a "
            "summary is caller-supplied content, so an amendment proposal "
            "composed against the previous revision goes stale rather than "
            "silently applying over the new summary (ADR 0028)."
        ),
    )


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


class VaultSpanChange(BaseModel):
    """A body change named as its old text rather than written as a patch.

    Not a stored kind. The service resolves the span against the note's body
    under the same lock that checked `base_revision`, writes the canonical
    unified diff, and stores an ordinary `body_diff` -- so a reviewer reads one
    artifact whichever way it was authored (ADR 0033).

    It exists over HTTP because a browser cannot honestly produce the
    alternative: generating a unified diff needs a diff implementation the page
    would have to hand-roll, and a selection with a replacement is exactly the
    two texts this carries (ADR 0039).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["span"]
    expected_text: str = Field(
        min_length=1,
        max_length=MAX_BODY_CHARS,
        description=(
            "The existing text to replace, copied verbatim -- whitespace "
            "included -- from the note as fetched."
        ),
    )
    replacement_text: str = Field(
        max_length=MAX_BODY_CHARS,
        description=(
            "What to put in its place. Empty removes the span, which counts "
            "as a removal at review time."
        ),
    )
    occurrence: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Which match to edit, 1-based, when the text is not unique. Omit "
            "it to require uniqueness: a span matching more than one place is "
            "refused rather than guessed at, and so is one matching none."
        ),
    )


class VaultMetadataUpdateRequest(BaseModel):
    """A metadata-only edit, applied directly by an update-scoped caller.

    Every field is optional and replaces the one it names. Absent means
    unchanged, an empty list means empty, and `clear_source_url` says which of
    those a null `source_url` meant -- over JSON the two are otherwise the same
    thing on the wire.

    `title`, `body`, `tags`, `aliases` and `summary` are absent by design. Each
    joins `assemble_embedding_text` or is content outright, and excluding them
    is what lets this path skip the re-embed and the dedup gate the full
    replacement runs. See ADR 0036.
    """

    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    related_ids: list[str] | None = Field(default=None, max_length=MAX_EDGE_IDS)
    source_ids: list[str] | None = Field(default=None, max_length=MAX_EDGE_IDS)
    facets: dict[str, list[str]] | None = None
    source_url: AnyUrl | None = None
    clear_source_url: bool = False

    @field_validator("related_ids", "source_ids")
    @classmethod
    def validate_ids(cls, ids: list[str] | None) -> list[str] | None:
        return None if ids is None else validate_edge_ids(ids)

    @model_validator(mode="after")
    def check_source_url_intent(self) -> "VaultMetadataUpdateRequest":
        validate_source_url_intent(self.source_url, self.clear_source_url)
        return self

    @field_validator("facets")
    @classmethod
    def validate_facet_shape(
        cls, facets: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        if facets is None:
            return None
        for name, values in facets.items():
            if not isinstance(values, list):
                raise ValueError(
                    f"facet {name!r} must be a list of strings, not a bare value"
                )
            if any(not str(value).strip() for value in values):
                raise ValueError(f"facet {name!r} must not contain empty values")
        return normalize_facets(facets)


class VaultMetadataChange(BaseModel):
    """Edges and classification only, with each field optional.

    An absent field means unchanged; an empty list or map means empty. That
    distinction is the point of the kind: a caller can add one edge without
    restating the note, and a reviewer sees the change rather than a document
    with four differences hidden in it.

    `tags` and `aliases` are absent on purpose -- both join the embedding text,
    so editing them alters what the note means to search and belongs on the
    replacement path that re-embeds and re-runs dedup. See vault ADR 0036.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["metadata"]
    related_ids: list[str] | None = Field(default=None, max_length=MAX_EDGE_IDS)
    source_ids: list[str] | None = Field(default=None, max_length=MAX_EDGE_IDS)
    facets: dict[str, list[str]] | None = None
    source_url: AnyUrl | None = None
    # Present here as well as on the applied request, because a REST proposer
    # could otherwise express every metadata change except removing a URL. A
    # null `source_url` over JSON is indistinguishable from an omitted one at
    # the transport, so the intent needs its own field rather than being
    # inferred.
    clear_source_url: bool = False

    @field_validator("related_ids", "source_ids")
    @classmethod
    def validate_ids(cls, ids: list[str] | None) -> list[str] | None:
        return None if ids is None else validate_edge_ids(ids)

    @model_validator(mode="after")
    def check_source_url_intent(self) -> "VaultMetadataChange":
        validate_source_url_intent(self.source_url, self.clear_source_url)
        return self

    @field_validator("facets")
    @classmethod
    def validate_facet_shape(
        cls, facets: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        if facets is None:
            return None
        for name, values in facets.items():
            if not isinstance(values, list):
                raise ValueError(
                    f"facet {name!r} must be a list of strings, not a bare value"
                )
            if any(not str(value).strip() for value in values):
                raise ValueError(f"facet {name!r} must not contain empty values")
        return normalize_facets(facets)

    @model_serializer(mode="wrap")
    def _only_what_the_proposal_changes(self, handler):  # type: ignore[no-untyped-def]
        """Render the fields the proposer set, and no others.

        Serializing the whole model turned a sparse change into four keys, three
        of them `null` by default -- so a reviewer could not tell an untouched
        `source_url` from a request to clear one, and the artifact this kind
        exists to make readable read as a document with differences in it.

        `kind` is always kept: it is the discriminator the union is decoded by,
        so dropping it would make the rendered change undecodable.
        """

        rendered = handler(self)
        return {
            key: value
            for key, value in rendered.items()
            if key == "kind" or key in self.model_fields_set
        }


VaultAmendmentChange = Annotated[
    VaultReplacementChange | VaultBodyDiffChange | VaultMetadataChange,
    Field(discriminator="kind"),
]

# What a *request* may carry, which is one kind more than a response can return.
# A span is an authoring form: it is resolved to a body diff before anything is
# stored, so no proposal ever reads back as one. Two unions rather than a shared
# one with a kind that never appears in a response -- a reader of the detail
# model should not have to work out which members are unreachable.
VaultAmendmentProposedChange = Annotated[
    VaultReplacementChange
    | VaultBodyDiffChange
    | VaultMetadataChange
    | VaultSpanChange,
    Field(discriminator="kind"),
]


class VaultAmendmentProposalRequest(BaseModel):
    """An immutable change proposed against one document revision."""

    model_config = ConfigDict(extra="forbid")

    target_note_id: str = Field(
        min_length=1, max_length=MAX_DOCUMENT_ID_CHARS
    )
    base_revision: int = Field(ge=1)
    change: VaultAmendmentProposedChange
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


class VaultQueuePreview(BaseModel):
    """What one queued proposal would do, sized to travel with the queue.

    A metadata preview is carried **in full**: it is small, and it is the whole
    proposal -- edges and classification with every id resolved to a title, so
    a reviewer can judge the row without opening it.

    A body change is carried as counts only. `resulting_body` and the unified
    diff stay behind `GET /amendment-proposals/{id}`, because a replacement may
    hold a hundred thousand characters and a queue of them is precisely the
    payload this exists to avoid sending.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    target_title: str | None = Field(
        default=None,
        description=(
            "The note's current title. Null when the target no longer "
            "resolves. Carried here because the queue is otherwise a list of "
            "ids, and the service has already fetched the document."
        ),
    )
    metadata_preview: "VaultMetadataPreview | None" = None
    added_line_count: int | None = None
    removed_line_count: int | None = None
    hunk_count: int | None = None
    requires_removal_acknowledgement: bool | None = None
    stale: bool = Field(
        default=False,
        description=(
            "The target has moved since this proposal was written, so no "
            "preview describes it: accepting would settle it as stale."
        ),
    )


class VaultAmendmentQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending: list[VaultAmendmentProposalSummary]
    count: int
    previews: list[VaultQueuePreview] = Field(
        default_factory=list,
        description=(
            "One entry per proposal in `pending`, same order. Present so a "
            "reviewer can triage the queue from one request rather than one "
            "per row."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Previews were dropped from the tail to fit a byte budget. The "
            "proposals themselves are all still listed; fetch the missing "
            "previews individually."
        ),
    )


class VaultAmendmentProposalDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: VaultAmendmentProposalSummary
    change: VaultAmendmentChange
    target: "VaultDocumentDetail | None"
    preview: "VaultAmendmentPreview | None"
    metadata_preview: "VaultMetadataPreview | None" = None


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


class VaultEdgeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = Field(
        default=None,
        description=(
            "The title this edge points at, or null when the id resolves to no "
            "document. Edges are not existence-checked on write (ADR 0025), so "
            "null means the reviewer should look: an archived note is "
            "legitimate, a note that was never written is a dangling edge."
        ),
    )


class VaultMetadataPreview(BaseModel):
    """What a metadata proposal changes, resolved for a human reviewer.

    Present only for `change_kind: "metadata"`. The sibling `preview` field
    describes a *body* change, which this kind cannot have -- it reports an
    empty diff, truthfully and uselessly. Read this instead.
    """

    model_config = ConfigDict(extra="forbid")

    related_added: list[VaultEdgeRef]
    related_removed: list[VaultEdgeRef]
    source_added: list[VaultEdgeRef]
    source_removed: list[VaultEdgeRef]
    related_reordered: bool = Field(
        default=False,
        description=(
            "Same edges, different order. Additions and removals are set "
            "differences and report nothing here, but the stored list is "
            "ordered, so accepting still rewrites it and advances the revision."
        ),
    )
    source_reordered: bool = False
    facets_before: dict[str, list[str]] | None = Field(
        default=None,
        description="Null when the proposal does not touch facets at all.",
    )
    facets_after: dict[str, list[str]] | None = None
    source_url_before: str | None = None
    source_url_after: str | None = None
    source_url_changed: bool
    changes_nothing: bool = Field(
        description=(
            "True when the proposal would leave the note exactly as it is. A "
            "reviewer can accept it harmlessly, but it is worth seeing: it "
            "usually means the change already landed by another route."
        )
    )


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


class VaultAmendmentBatchDecisionItem(VaultAmendmentDecisionRequest):
    """One independently settled decision in a bounded batch."""

    proposal_id: UUID


class VaultAmendmentBatchDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[VaultAmendmentBatchDecisionItem] = Field(
        min_length=1,
        max_length=MAX_AMENDMENT_BATCH_DECISIONS,
    )

    @model_validator(mode="after")
    def proposal_ids_are_unique(self) -> "VaultAmendmentBatchDecisionRequest":
        proposal_ids = [item.proposal_id for item in self.decisions]
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("proposal_id values must be unique within a batch")
        return self


class VaultAmendmentBatchDecisionResult(BaseModel):
    """The compact result of one batch item.

    Batch decisions are intentionally independent: a stale or concurrently
    settled proposal does not roll back decisions that already succeeded.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    outcome: Literal["accepted", "rejected", "stale"] | None = None
    status_code: int
    detail: str | dict[str, Any] | None = None


class VaultAmendmentBatchDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[VaultAmendmentBatchDecisionResult]
    decided: int
    refused: int


class VaultContributionResponse(BaseModel):
    """Outcome of a governed write.

    Mirrors the `vault.contribute` output in the MCP tool schema. Note that
    `flagged` is a successful write, not a failure: the note landed and a
    review case was opened. Callers branch on `status`, so it is deliberately
    a small closed vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["inserted", "flagged", "rejected", "invalid"] = Field(
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
    max_similarity: VaultSimilarNote | None = Field(
        default=None,
        description=(
            "The single closest existing NOTE, which is the dedup gate's "
            "verdict: how near this contribution came to being flagged, and "
            "what it came near to. Null when the corpus held nothing to score "
            "against. Always present regardless of `response_detail` -- it is "
            "the one piece of evidence a contributor acts on."
        ),
    )
    summary_advice: str | None = Field(
        default=None,
        description=(
            "Present only when a note landed without a summary. An instruction "
            "rather than a complaint: it names the call that supplies one and "
            "the window that call stays open for. Null when a summary was "
            "given, and null when nothing was written."
        ),
    )
    similars: list[VaultSimilarNote] = Field(
        default_factory=list,
        description=(
            "Existing NOTES the candidate scored against, ranks 2..n. Empty "
            "unless `response_detail=review` was asked for: the contributor "
            "searched moments ago, and `max_similarity` already carries the "
            "verdict, so the rest is a list of note ids inviting a fetch "
            "nobody needed. `note_id` is resolvable with GET /notes/{id}."
        ),
    )
    related_pages: list[VaultSimilarNote] = Field(
        default_factory=list,
        description=(
            "Compiled WIKI PAGES near this note. CONTEXT, NOT A VERDICT: a "
            "page restates the notes it was built from, so resembling one is "
            "expected and is never why a contribution is flagged. Empty unless "
            "`response_detail=review` was asked for -- on a write response it "
            "is scored note ids that had no bearing on the outcome. Useful for "
            "deciding whether to extend an existing synthesis, which is a "
            "reviewing question rather than a contributing one. Every entry is "
            "a wiki page by construction -- the query filters on kind -- and "
            "`note_id` carries its document id, the same id space every other "
            "surface uses and resolvable with GET /notes/{id}."
        ),
    )
    errors: list[str] = Field(default_factory=list)


class VaultContributionDetail(StrEnum):
    """How much of the dedup gate's working to return.

    Two callers want different things from one write. A programmatic reviewer
    building an adjudication surface wants every candidate the gate weighed; an
    agent that just contributed wants to know what happened. The default
    therefore differs by transport rather than being one compromise for both --
    see `contribution_response`.
    """

    OUTCOME = "outcome"
    REVIEW = "review"


def _summary_advice(
    outcome: Any,
    *,
    supplied: bool,
    operation: str | None,
) -> str | None:
    """What to tell a contributor that omitted the summary. ADR 0035.

    Only on ``inserted``, and the exclusions are the interesting part. A
    ``flagged`` note is written but withheld from the read surface pending
    adjudication -- ``READABLE_STATUSES`` is active and archived -- so the
    carveout would 404 on it, and advice naming a call that cannot succeed is
    worse than no advice. ``rejected`` and ``invalid`` wrote nothing at all.

    A replay is answered from the note's *current* state rather than from the
    fact of being a replay. Suppressing it outright assumed the caller had
    already seen the first response, which is exactly what a caller retrying a
    lost one has not: the replay is then its only observed response, and it
    would never learn the note is undescribed or that the window is running.
    ``summary_repairable`` is the service's answer to "would
    ``vault_set_summary`` still succeed on this note", so the advice appears
    while the repair is possible and stops once it is not.

    ``operation`` is the caller's own name for the follow-up, supplied by the
    adapter, because one builder serves a tool surface and an HTTP surface that
    do not share a vocabulary. The *rule* stays here so the two cannot drift
    about when a contributor gets told.
    """

    if supplied or operation is None:
        return None
    if outcome.status != "inserted" or outcome.note_id is None:
        return None
    if outcome.idempotent_replay and not getattr(
        outcome, "summary_repairable", False
    ):
        return None

    # Deliberately terse, and the terseness is measured. This fires on almost
    # every contribution today -- 3 notes in 70 carry a summary -- and every
    # byte here is paid twice, in `structuredContent` and in the text block.
    # A first draft explaining *why* a summary matters cost 740 bytes and put
    # the write response 53% over the budget `test_mcp_budget` pins. The
    # reasoning belongs in the `vault_set_summary` description, which the model
    # reads once when it decides to call the tool, rather than in a response it
    # receives every time. What has to be here is the instruction: the verb,
    # the operation, and the deadline. The note id is not repeated because
    # `note_id` is already a field of this response.
    minutes = SUMMARY_GRACE_PERIOD_SECONDS // 60
    return f"No summary. Add one with {operation} within {minutes} minutes."


def contribution_response(
    outcome: Any,
    *,
    detail: VaultContributionDetail,
    summary_supplied: bool = True,
    summary_operation: str | None = None,
) -> VaultContributionResponse:
    """Render a settled write, at the asked-for level of detail.

    Shared by both adapters, like ``search_response`` and for the same reason.

    `max_similarity` survives at every detail level because it is the verdict:
    it names what the candidate came closest to and how close, which is the
    fact a contributor acts on. What `review` adds is the gate's *working* --
    the other four notes it weighed, and the wiki pages near the result, which
    `app/vault/AGENTS.md` is explicit are context and never a reason anything
    was flagged.

    At `review` detail `similars` keeps the **whole** list, rank 1 included,
    so `max_similarity` is purely additive there and no existing caller loses
    a field. Deduplicating it would have been tidier and would have quietly
    changed a shipped contract, which is the worse trade.

    The similars are already ordered by score, so the maximum is the first.
    Recomputing it with `max()` would invite the ordering to drift out from
    under this function silently.
    """

    similars = [
        VaultSimilarNote(note_id=s.note_id, title=s.title, score=s.score)
        for s in outcome.similars
    ]
    pages = [
        VaultSimilarNote(note_id=s.note_id, title=s.title, score=s.score)
        for s in outcome.related_pages
    ]
    reviewing = detail is VaultContributionDetail.REVIEW

    return VaultContributionResponse(
        status=outcome.status,
        note_id=outcome.note_id,
        message=outcome.message,
        idempotent_replay=outcome.idempotent_replay,
        max_similarity=similars[0] if similars else None,
        summary_advice=_summary_advice(
            outcome, supplied=summary_supplied, operation=summary_operation
        ),
        similars=similars if reviewing else [],
        related_pages=pages if reviewing else [],
        errors=list(outcome.errors),
    )


class VaultRetirementResponse(BaseModel):
    """The settled outcome of retiring one note.

    `retired` is always true: the tool raises rather than returning false, so
    a caller that got a response got a retirement. It is present because a
    bare `{note_id}` reads as a lookup rather than an outcome.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    retired: bool = True


class VaultPromotionResponse(BaseModel):
    """Where a note ended up after its promotion status changed.

    `vault_path` is returned because changing promotion can move the document
    between the export's engine-managed folders (ADR 0023), and `moved` says
    whether it did -- setting a status to the value it already held is a
    no-op, not a failure.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    promotion_status: str | None
    vault_path: str
    moved: bool


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


class VaultSearchHit(BaseModel):
    """One candidate, described well enough to choose between candidates.

    Deliberately **not** a `VaultDocumentDetail`. It was one until 2026-08-26,
    which made search and fetch return the same thing and turned choosing one
    note out of ten into paying for ten: a ten-hit page cost 58,784 bytes on
    the wire, most of it bodies the caller discarded. Discovery and retrieval
    are now separate operations with separate costs -- this names candidates,
    and `vault_get_note` returns one.

    What a field has to earn to be here is *selection* value: it must help
    decide which note to open. `body`, `tags`, `aliases`, `facets`,
    `related_ids`, `source_ids`, `vault_path` and the timestamps were all
    removed against that test. They are a fetch away, and nine hits out of ten
    never need them. Adding a field back needs a demonstrated selection need,
    not a caller who finds it convenient.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    summary: str | None = Field(
        default=None,
        description=(
            "The note's authored precis, when it has one. Prefer this over "
            "`snippet`: it describes the whole document, where a snippet only "
            "opens it."
        ),
    )
    snippet: str | None = Field(
        default=None,
        description=(
            "A bounded extract of the note's opening, supplied only when "
            "`summary` is absent. Read `summary or snippet` -- but a note "
            "whose body is entirely headings, code or a table has no prose to "
            "extract, so a hit may carry neither and the title is then the "
            "only description. A blank string rather than null would read as "
            "a preview that was computed and came back empty. "
            "Deliberately NOT a match highlight: a hit found by "
            "the vector arm shares no vocabulary with the query, so nothing "
            "in it could be highlighted, and a field meaning one thing for "
            "lexical hits and another for semantic ones would be worse than "
            "one meaning the same for both. An ellipsis marks a clipped "
            "extract."
        ),
    )
    kind: DocumentKind = Field(
        description=(
            "`note` or `wiki`. A wiki page restates the notes it was compiled "
            "from, so prefer a note when one answers the question and open a "
            "page when the synthesis itself is what you need."
        ),
    )
    doc_status: str | None = Field(
        default=None,
        description=(
            "Status Map value from types.yml, for example 'Evergreen' or "
            "'Stub'. Useful for judging how much weight a candidate carries."
        ),
    )
    content_revision: int = Field(
        ge=1,
        description=(
            "Monotonic content version at the time of the search. Amending a "
            "note still requires fetching it first -- this is a staleness "
            "hint, not a substitute for the fetched value."
        ),
    )
    score: float = Field(
        description=(
            "Reciprocal rank fusion score. Orders hits **within this "
            "response only**. It is derived from positions in this query's "
            "candidate set, not from a property of the document, so it is not "
            "comparable across queries and no fixed value means 'relevant'."
        ),
    )
    lexical_rank: int | None = Field(
        default=None,
        description="1-based position in the full-text ranking, if it matched.",
    )
    vector_rank: int | None = Field(
        default=None,
        description="1-based position in the vector ranking, if it matched.",
    )


class VaultNoteSummary(BaseModel):
    """One row of a browse listing: enough to choose a note, and no body.

    The same discipline as ``VaultSearchHit`` -- what earns a field here is
    *selection* value -- with one deliberate difference. ``vault_path`` was
    removed from a search hit because a fused ranking is not a place; it is the
    organising key of a listing, and a browse response without it would name
    notes the caller cannot locate.

    No snippet. A listing is read by someone who is already looking at a
    folder, so the title and the path carry most of the meaning, and computing
    an extract for every row of every page costs more than it tells. A note
    with no summary is opened, not guessed at.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    vault_path: str = Field(
        description=(
            "Vault-root-relative posix path: where the note lives, and what "
            "the default listing is ordered by. Not a paging cursor -- it was "
            "one until ADR 0045, and `next_cursor` carries the position now."
        )
    )
    kind: DocumentKind
    status: DocumentStatus
    doc_type: str | None = None
    doc_status: str | None = None
    summary: str | None = Field(
        default=None,
        description=(
            "The note's authored precis, when it has one. Null is ordinary "
            "rather than an error: not every note carries one."
        ),
    )
    updated_at: datetime
    content_revision: int = Field(
        ge=1,
        description=(
            "Monotonic content version at the time of the listing. Amending a "
            "note still requires fetching it first -- this is a staleness "
            "hint, not a substitute for the fetched value."
        ),
    )


class VaultNoteEdge(BaseModel):
    """One resolved edge: an id, and the two ways a person refers to it.

    Deliberately smaller than ``VaultNoteSummary``. This answers "what is this
    id called", which a link label needs; it is not a listing row and must not
    become one by accretion, or every surface showing edges pays a listing's
    weight to draw a few link texts.

    ``slug`` rather than ``vault_path`` because the slug is what a wikilink
    carries (ADR 0022's amendment, and what the export writes), and because a
    path is a location the caller has no use for here.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    title: str
    slug: str = Field(
        description=(
            "The leaf of `vault_path`, which is the title's slug -- the name a "
            "`[[wikilink]]` to this note carries."
        ),
    )


class VaultNoteEdgeLookupRequest(BaseModel):
    """The ids whose names a caller wants.

    A body rather than a query string, and that is a transport decision with a
    reason. Ids are not length-bounded anywhere -- `validate_edge_ids` checks
    shape and uniqueness, not size -- so a hundred of them in a query string
    can exceed the 8,192-byte request line Heroku's router accepts, and the
    request fails before any handler sees it. A body has no such ceiling.

    Deliberately *not* run through `validate_edge_ids`. This reads what the
    corpus already holds, including rows written before that rule existed;
    refusing to look up a malformed id would withhold names from exactly the
    notes that need repairing.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_EDGE_LOOKUP_IDS,
        description=(
            "Note ids to resolve. Unknown or unreadable ids are absent from "
            "the response rather than reported."
        ),
    )


class VaultNoteEdgeResponse(BaseModel):
    """Every requested id the caller may read, in no meaningful order.

    An id that resolved to nothing readable is absent rather than reported: see
    the endpoint, where that is a disclosure decision rather than a shape one.
    The caller holds the ids it asked for and indexes this by `note_id`.
    """

    model_config = ConfigDict(extra="forbid")

    edges: list[VaultNoteEdge]


class VaultNoteListResponse(BaseModel):
    """One ordered page of notes, paged by an opaque cursor.

    Ordered by ``vault_path`` today, and the order is about to become a
    request parameter (ADR 0045), which is exactly why ``next_cursor`` no
    longer spells the key it stopped at.
    """

    model_config = ConfigDict(extra="forbid")

    notes: list[VaultNoteSummary]
    has_more: bool = Field(
        default=False,
        description=(
            "Whether another page exists under the same filters. Determined by "
            "reading one row past the limit, so it is a fact about the corpus "
            "rather than a guess from a full page."
        ),
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "The token to pass back as `after` for the next page, or null at "
            "the end. Opaque: it names a position in one ordered walk, and is "
            "not a vault_path, a note id, or anything else to build or read "
            "(ADR 0045). It was a vault_path until then; sending one now is a "
            "422, so pass this back verbatim. Keyset rather than an offset, so "
            "rows inserted behind the cursor cannot shift the walk. It is not "
            "a snapshot: the key it names is mutable -- promotion moves a "
            "note's vault_path on purpose -- so a row that crosses the cursor "
            "between calls can be seen twice or not at all. For browsing that "
            "is a refresh; for anything that must be exact, read the export."
        ),
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
    has_more: bool = Field(
        default=False,
        description=(
            "Whether fusion ranked further hits below this page, within the "
            "candidate window each arm retrieves. Not corpus exhaustion: "
            "`false` means nothing more was ranked here, not that nothing "
            "else in the corpus matches. For deciding whether to narrow a "
            "query those are the same answer."
        ),
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Reserved, and always null today. Resuming a page needs a total "
            "order that is stable between calls, and this ranking has none: a "
            "fused score is computed from positions within one query's "
            "candidate set, so inserting a document can move every score "
            "below it, and a document outside the candidate window has no "
            "score at all. Populating this with an offset would promise "
            "stability the ranking cannot deliver -- narrow the query instead."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Whether hits were dropped from this page to keep the response "
            "inside its byte budget. Distinct from `has_more`, which is about "
            "additional results ranked within the candidate window: "
            "`truncated` means this response is smaller than the limit asked "
            "for. Narrow the query or lower the limit."
        ),
    )


def search_hit(
    document: VaultDocument,
    *,
    score: float,
    lexical_rank: int | None,
    vector_rank: int | None,
) -> VaultSearchHit:
    """Project a document onto one search candidate.

    The counterpart of ``document_detail`` and shared by both adapters for the
    same reason: two copies of this projection would let the HTTP and MCP
    search surfaces drift into disagreeing about what a hit is.

    The snippet is computed only when there is no authored summary. Sending
    both would spend bytes restating the same thing -- and it would do it
    exactly where it is least useful, since summary coverage is concentrated
    in wiki pages (14 of 15) and nearly absent from notes (3 of 70).
    """

    summary = document.summary
    return VaultSearchHit(
        note_id=document.id,
        title=document.title,
        summary=summary,
        snippet=None if summary else lead_snippet(document.body),
        kind=document.kind,
        doc_status=document.doc_status,
        content_revision=document.content_revision,
        score=score,
        lexical_rank=lexical_rank,
        vector_rank=vector_rank,
    )


# The byte budget one search response's structured payload is trimmed to. The
# wire
# carries roughly twice this, because the MCP transport also serializes the
# same object into a compatibility text block -- see `tests/vault/
# test_mcp_budget.py`, which measures both.
#
# 8 KiB is the efficiency assessment's acceptance criterion for ten ordinary
# hits. It is a budget, not a target: a normal ten-hit page costs about
# 5 KiB, so truncation should be something a caller provokes with `limit=50`
# rather than something they meet by accident.
#
# Not an unconditional ceiling. A single hit is never dropped, so a one-hit
# response may exceed this and reports `truncated=false` -- correctly, since
# nothing was dropped. Sizing a buffer from this constant is sizing it for
# the multi-hit case.
SEARCH_STRUCTURED_BUDGET_BYTES = 8 * 1024


def search_response(
    *,
    query: str,
    profile_id: str | None,
    vector_status: VectorSearchStatus,
    results: Sequence[SearchResult],
    has_more: bool,
    budget_bytes: int = SEARCH_STRUCTURED_BUDGET_BYTES,
) -> VaultSearchResponse:
    """Assemble one search response, trimmed to fit its byte budget.

    Shared by both adapters rather than written twice, for the reason
    ``canonical_request_digest`` is: two copies of a response contract drift,
    and a search that means different things over HTTP and MCP is the kind of
    difference nobody notices until a client depends on it.

    **Why a budget exists at all.** `limit` accepts up to 50. Fifty hits at
    the per-hit cost of a summary or snippet is several times what a page of
    ten costs, and the caller who asked for fifty is rarely the caller who
    wanted to spend that. Dropping the tail and saying so is better than
    either silently returning it or refusing the request: the top of a fused
    ranking is the part that was worth having.

    Trimming from the end is what makes that safe -- fusion has already
    ordered the hits, so the dropped ones are the lowest-ranked. At least one
    hit always survives, even one that exceeds the budget alone: a response
    with no hits would misreport a search that did match something.

    **A best-effort page budget, not an unconditional ceiling.** Precisely: a
    response of two or more hits is trimmed until it fits, and reports
    ``truncated=true`` exactly when hits were dropped. A response of one hit is
    never trimmed, so it may exceed the budget and still report
    ``truncated=false`` -- which is honest, because nothing was dropped. The
    measurement is of the whole response, envelope included, rather than of its
    hits alone.
    """

    hits = [
        search_hit(
            result.document,
            score=result.score,
            lexical_rank=result.lexical_rank,
            vector_rank=result.vector_rank,
        )
        for result in results
    ]

    def _assemble(
        candidates: list[VaultSearchHit], *, truncated: bool
    ) -> VaultSearchResponse:
        return VaultSearchResponse(
            query=query,
            profile_id=profile_id,
            vector_status=vector_status,
            hits=candidates,
            # A trimmed page always has more below it, whatever fusion reported.
            has_more=has_more or truncated,
            next_cursor=None,
            truncated=truncated,
        )

    def _size(candidate: VaultSearchResponse) -> int:
        """The whole response, because the whole response is what is sent.

        Measuring only `hits` left the envelope unbudgeted, and the envelope is
        not small: `query` is echoed back at up to
        ``SEARCH_QUERY_MAX_CHARS`` and `profile_id` is unbounded model naming.
        Ten hits at accepted field sizes cleared the hit budget and still
        exceeded the ceiling once those were added, reporting
        ``truncated=false`` while doing it.
        """

        return len(
            json.dumps(
                candidate.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    truncated = False
    while len(hits) > 1 and _size(_assemble(hits, truncated=truncated)) > budget_bytes:
        hits.pop()
        truncated = True

    return _assemble(hits, truncated=truncated)


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


def note_summary(document: VaultDocumentBrief) -> VaultNoteSummary:
    """Project one listing row.

    Beside ``document_detail`` and ``search_hit`` rather than inside the
    router, for the reason they are: a projection written at a transport is a
    projection that drifts from the other transports.

    Takes a ``VaultDocumentBrief`` rather than a ``VaultDocument`` so the type
    says what the query reads. A body cannot be dropped here because there is
    never one to drop.
    """

    return VaultNoteSummary(
        note_id=document.id,
        title=document.title,
        vault_path=document.vault_path,
        kind=document.kind,
        status=document.status,
        doc_type=document.doc_type,
        doc_status=document.doc_status,
        summary=document.summary,
        updated_at=document.updated_at,
        content_revision=document.content_revision,
    )


def note_edge(document: VaultDocumentBrief) -> VaultNoteEdge:
    """Project one resolved edge.

    Beside the other projections for the reason they are all here: a projection
    written at a transport drifts from the other transports. `slug_of` is the
    same helper the export renders `[[slug]]` with, so a link in the console
    and a link in the exported tree name a note identically.
    """

    return VaultNoteEdge(
        note_id=document.id,
        title=document.title,
        slug=slug_of(document.vault_path),
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
    if proposal.change_kind is AmendmentProposalKind.METADATA:
        return VaultMetadataChange(kind="metadata", **proposal.change)
    return VaultReplacementChange(
        kind="replacement",
        replacement=VaultDocumentUpdateRequest.model_validate(proposal.change),
    )


# The queue's preview budget. Metadata previews are small -- forty-four of them
# measured at 8KB of change payload -- so this is not sized to ration them. It
# bounds the pathological case: `limit` accepts 200, and a queue of proposals
# each carrying many renamed edges could otherwise assemble a response nobody
# asked for. Previews are dropped from the tail and the drop is reported; the
# proposals themselves are never dropped.
QUEUE_PREVIEW_BUDGET_BYTES = 256 * 1024


def amendment_queue_previews(
    proposals: Sequence[VaultAmendmentProposal],
    previews: dict[UUID, Any],
    target_titles: dict[str, str] | None = None,
    budget_bytes: int = QUEUE_PREVIEW_BUDGET_BYTES,
) -> tuple[list[VaultQueuePreview], bool]:
    """Project the queue's previews, trimmed from the tail to fit.

    Trimming the tail is what makes the budget safe: `list_pending` is oldest
    first, so the rows most at risk of being forgotten keep their previews and
    the newest lose them. A dropped preview costs one extra request, never a
    missing proposal.
    """

    assembled: list[VaultQueuePreview] = []
    spent = 0
    truncated = False
    titles = target_titles or {}
    for proposal in proposals:
        summary = previews.get(proposal.id)
        title = titles.get(proposal.target_document_id)
        if isinstance(summary, MetadataChangeSummary):
            item = VaultQueuePreview(
                proposal_id=proposal.id,
                target_title=title,
                metadata_preview=amendment_metadata_preview(summary),
            )
        elif isinstance(summary, BodyChangeSummary):
            item = VaultQueuePreview(
                proposal_id=proposal.id,
                target_title=title,
                added_line_count=summary.added_line_count,
                removed_line_count=len(summary.removed_lines),
                hunk_count=summary.hunk_count,
                requires_removal_acknowledgement=(
                    summary.requires_removal_acknowledgement
                ),
            )
        else:
            item = VaultQueuePreview(
                proposal_id=proposal.id, target_title=title, stale=True
            )

        spent += len(item.model_dump_json().encode("utf-8"))
        if spent > budget_bytes and assembled:
            truncated = True
            break
        assembled.append(item)
    return assembled, truncated


def amendment_metadata_preview(
    summary: MetadataChangeSummary | None,
) -> VaultMetadataPreview | None:
    if summary is None:
        return None

    def refs(items: tuple[EdgeRef, ...]) -> list[VaultEdgeRef]:
        return [VaultEdgeRef(id=item.id, title=item.title) for item in items]

    return VaultMetadataPreview(
        related_added=refs(summary.related_added),
        related_removed=refs(summary.related_removed),
        source_added=refs(summary.source_added),
        source_removed=refs(summary.source_removed),
        related_reordered=summary.related_reordered,
        source_reordered=summary.source_reordered,
        facets_before=summary.facets_before,
        facets_after=summary.facets_after,
        source_url_before=summary.source_url_before,
        source_url_after=summary.source_url_after,
        source_url_changed=summary.source_url_changed,
        changes_nothing=summary.is_empty,
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
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    source_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_PAGE_EDGE_IDS,
        description=(
            "The notes this page was synthesized from. Validated: unlike a "
            "note's related_ids, provenance naming something that does not "
            "exist is refused rather than stored."
        ),
    )
    summary: str | None = Field(default=None, max_length=2_000)
    tags: list[str] = Field(default_factory=list)
    related_ids: list[str] = Field(
        default_factory=list, max_length=MAX_PAGE_EDGE_IDS
    )
    page_id: str | None = Field(
        default=None,
        description=(
            "Rewrite this existing page rather than creating one. From the "
            "plan item's `page_id`."
        ),
    )

    @field_validator("related_ids", "source_ids")
    @classmethod
    def validate_ids(cls, ids: list[str]) -> list[str]:
        """The same rule every other edge-writing surface obeys.

        This model shipped without it, so a compiled page could store blanks,
        duplicates, titles and `[[wikilinks]]` in `related_ids` -- exactly the
        corruption ADR 0030 draws the line against and
        `scripts/resolve_vault_wikilinks.py` exists to repair, reintroduced on
        a path that writes to the corpus. Duplicate `source_ids` also slipped
        past the existence check, since resolving them collapses the list.

        Deliberately no `max_length` beside it. The other write models cap
        edges at 50; a compiled page is synthesized *from* notes and may
        legitimately name many more, so a cardinality bound here is a separate
        decision about what a page may cite -- not a number to inherit by
        copying the line above.
        """

        return validate_edge_ids(ids)


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
