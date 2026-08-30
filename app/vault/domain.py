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


class PromotionStatus(str, Enum):
    """Whether a note has been proposed for the Human layer, and what came of it.

    Distinct from ``DocumentStatus`` (the vault's visibility gate) and from
    ``doc_status`` (the Status Map value): three different questions, none
    derived from the others (ADR 0011's rule, applied a third time). ``None``
    is a real value here and the ordinary one -- never proposed.

    The export routes on ``CANDIDATE`` alone. The other two exist so the
    *outcome* is recorded rather than collapsing back into "never proposed",
    which is what stops a note being re-proposed forever and lets a reviewer
    see that something was already considered and declined. Same shape as a
    review case, where ``accepted`` and ``rejected`` both mean settled and are
    worth telling apart. See ADR 0023.
    """

    # Proposed for promotion, awaiting human judgement. Exports to
    # `Agent/Promotion Candidates/`.
    CANDIDATE = "candidate"
    # A Human note has been written from it. The agent note is *not* consumed:
    # promotion rewrites rather than moves, so the original stays a
    # first-class note in `Agent/notes/`.
    PROMOTED = "promoted"
    # Considered and declined. Back to `Agent/notes/`, and not a candidate
    # again without a fresh judgement.
    RETRACTED = "retracted"


class ReviewState(str, Enum):
    """What a human decided about a flagged contribution.

    The enum shipped with the schema carrying no defined meaning, which is how
    it came to be read two ways. These are the definitions, and they are about
    **the note**, not about the case -- matching the only other use of
    "rejected" in this package, where a rejected contribution is one the write
    path refused.

    A candidate is always a *brand-new* note. ``insert_pending`` is called from
    exactly one place, the contribute path's ``Flag`` branch, with the note it
    has just written; pre-existing notes appear only as evidence inside
    ``similar_documents``, and the update path refuses on collision rather than
    opening a case. So a decision is always about content that has never been
    endorsed, which is what makes ``REJECTED`` a deletion rather than an
    archival: ADR 0019 archives what is overtaken and deletes what is wrong, and
    a duplicate judged redundant at birth has no history to preserve.
    """

    PENDING = "pending"
    # The flag was a false positive: the note is legitimate. It becomes active
    # and re-enters search and the dedup corpus.
    ACCEPTED = "accepted"
    # The note really is a duplicate of something already in the corpus. It is
    # deleted, and this case survives with a null candidate to say so.
    REJECTED = "rejected"
    # Reserved. No decision path sets it, and none should until someone has a
    # case that needs it and a reason to write down -- an enum with invented
    # semantics is what this docstring exists to correct.
    SUPERSEDED = "superseded"


class AmendmentProposalState(str, Enum):
    """Lifecycle of an immutable proposed change to an existing note."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


class AmendmentProposalKind(str, Enum):
    """Closed set of changes an amendment proposal may carry."""

    REPLACEMENT = "replacement"
    BODY_DIFF = "body_diff"
    # Edges and classification only -- the fields that do not join the
    # embedding text. See vault ADR 0036.
    METADATA = "metadata"


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
    # Vault-root-relative posix path. Required: it is the key tying this row
    # to its file, so a document without one cannot be projected. See ADR 0010.
    vault_path: str
    title: str
    body: str
    contributed_by: str
    provenance: dict[str, Any]
    schema_version: int
    created_at: datetime
    updated_at: datetime
    # Monotonic content version used by amendment proposals as their compare-
    # and-swap token. Lifecycle-only changes do not move it.
    content_revision: int = 1
    # Governance Type Dictionary value, validated against types.yml at the
    # write boundary rather than here. None means untyped, which is a real
    # state rather than missing data. See ADR 0009.
    doc_type: str | None = None
    # Status Map value from types.yml, distinct from `status`. See ADR 0011.
    doc_status: str | None = None
    # Promotion candidacy, distinct from both of the above. None means never
    # proposed, which is the ordinary state. See ADR 0023.
    promotion_status: PromotionStatus | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # Frontmatter keys the schema does not model, kept so the projector can
    # re-emit a note the validator accepts. See ADR 0013.
    frontmatter: dict[str, Any] = field(default_factory=dict)
    # Classification relating this note to others -- {"project": ["hss"]}.
    # Outside the embedding text by construction. See ADR 0017.
    facets: dict[str, list[str]] = field(default_factory=dict)
    # Upstream provenance for content authored before it reached this vault --
    # {"author": "agent:codex", "created_at": "..."}. Empty means this vault is
    # the origin. See origin.py and migration 0010.
    origin: dict[str, str] = field(default_factory=dict)
    # SHA-256 of the upstream file; None when the row has no upstream file
    # because it was authored here. See ADR 0012.
    source_sha256: bytes | None = None
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    compile_run_id: UUID | None = None
    compiled_by: str | None = None
    compiled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewVaultDocument:
    """Content on its way into a row, from a contribution or a replacement.

    Deliberately carries no ``promotion_status``: a note is never a candidate
    at birth, and an update is a new body for an existing row rather than a new
    judgement about it. Candidacy is set afterwards by a reviewer holding
    ``vault:review``, together with the ``vault_path`` it routes to, and
    keeping the field off this record is what makes that the only way in.
    """

    id: str
    kind: DocumentKind
    status: DocumentStatus
    vault_path: str
    title: str
    body: str
    contributed_by: str
    provenance: dict[str, Any]
    schema_version: int = 1
    doc_type: str | None = None
    doc_status: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    frontmatter: dict[str, Any] = field(default_factory=dict)
    # See ADR 0017. Never reaches assemble_embedding_text.
    facets: dict[str, list[str]] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    source_sha256: bytes | None = None
    related_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_url: str | None = None
    compile_run_id: UUID | None = None
    compiled_by: str | None = None
    compiled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NoteCompileState:
    """What the compile planner needs to know about one note.

    Three fields rather than a whole ``VaultDocument`` because staleness and
    coverage are decided by when a note moved, whether it is still endorsed, and
    whether a compiler already declined it -- loading bodies to answer that would
    read the corpus into memory for nothing.

    A named type rather than a tuple: the third field arrived when declines
    replaced the frontier, and ``(updated_at, status, declined_at)`` positionally
    is the kind of thing a reader has to go and check.
    """

    updated_at: datetime
    status: str
    # When a compiler was shown this note and decided against a page. None is
    # the ordinary state. Stale -- and therefore ignored -- once `updated_at` is
    # later than it: a note that changed since the judgement is a different note.
    declined_at: datetime | None = None

    @property
    def declined(self) -> bool:
        """Whether the decline still stands.

        The comparison is the whole rule. The frontier it replaced got this for
        free, because a note editing itself past the frontier was re-offered by
        construction; stating it is better than inheriting it.
        """

        return self.declined_at is not None and self.updated_at <= self.declined_at


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
    # SHA-256 of the text this vector was built from. None means unknown, which
    # a re-embed job treats as stale. See ADR 0013.
    text_sha256: bytes | None = None


@dataclass(frozen=True, slots=True)
class VaultReviewCase:
    id: UUID
    # None once the candidate has been retired. The judgement is the durable
    # record; what it judged is allowed to be gone. See migration 0011.
    candidate_document_id: str | None
    state: ReviewState
    reason: str
    similar_documents: tuple[dict[str, Any], ...]
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


@dataclass(frozen=True, slots=True)
class VaultAmendmentProposal:
    """An immutable candidate change awaiting bounded adjudication."""

    id: UUID
    target_document_id: str
    target_revision: int
    change_kind: AmendmentProposalKind
    change: dict[str, Any]
    rationale: str
    state: AmendmentProposalState
    proposed_by: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    applied_revision: int | None = None
    removals_acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    pool_size: int
    checked_out: int
    maximum_checked_out: int
    checkout_count: int
    checkin_count: int
    checkout_failures: int
    latest_checkout_seconds: float | None
    maximum_checkout_seconds: float | None
    total_checkout_seconds: float = field(repr=False)


@dataclass(frozen=True, slots=True)
class RegisteredOAuthClient:
    """One dynamically registered OAuth client.

    ``client_info`` is the SDK's ``OAuthClientInformationFull`` as plain JSON,
    not a parsed model: persistence must not import a transport package, and
    RFC 7591 lets a registration carry metadata this schema never anticipated.
    The provider validates it back into the SDK model at its own boundary,
    which is the same division ``frontmatter`` JSONB already draws.

    ``expires_at`` is the client secret's expiry. Open registration means
    unbounded rows, so they are pruned; None is a client that does not expire,
    which is an operator's deliberate choice rather than a default.
    """

    client_id: str
    client_info: dict[str, Any]
    registered_at: datetime
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """One operator-approved OAuth authorization and its rotating family.

    ``authorized_scopes`` came through OAuth consent and is restricted to the
    baseline. ``entitled_scopes`` came only from the operator CLI. Keeping the
    two sets distinct prevents a refresh request from manufacturing privilege
    while allowing operator authority to survive access-token rotation.
    """

    family_id: UUID
    client_id: str
    authorized_scopes: tuple[str, ...]
    entitled_scopes: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """An authorization waiting for the operator to return from the login form.

    The vault mints a nonce, redirects to its own page carrying it, and finds
    this row again when the form posts back. Only ``sha256(nonce)`` is stored
    (ADR 0015's rule for machine-generated secrets), and redemption is a
    ``DELETE ... RETURNING`` so a replay finds nothing.

    ``params`` holds the SDK's ``AuthorizationParams`` as JSON -- including the
    PKCE ``code_challenge``, which waits here until ``/token`` redeems the code
    this becomes.
    """

    client_id: str
    params: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    # SHA-256 of the token the login form carries in a hidden field. None only
    # for a row written before migration 0014; the login route refuses that as
    # it would refuse a mismatch, rather than treating absence as permission.
    csrf_sha256: bytes | None = None


@dataclass(frozen=True, slots=True)
class StoredAuthorizationCode:
    """A minted authorization code, between the login form and ``/token``.

    Field-for-field the SDK's ``AuthorizationCode`` minus the code itself,
    which is never stored in the clear. ``subject`` records which identity
    method authenticated the operator, so an audit can tell a Google login from
    a password one after the fact.
    """

    client_id: str
    scopes: tuple[str, ...]
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    created_at: datetime
    expires_at: datetime
    resource: str | None = None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class StoredRefreshToken:
    """A refresh token, rotated on every use.

    ``family_id`` is constant across every rotation descending from one
    authorization. It exists so that presenting an already-consumed token --
    positive evidence that a token was captured -- can revoke the whole chain
    rather than merely failing one request, which is what OAuth 2.1 means by
    rotation *with replay detection*.

    ``consumed_at`` is set rather than the row deleted, for the same reason:
    a deleted row is indistinguishable from a token that never existed, and
    the distinction is the entire security property here. The other two
    transient OAuth tables delete on redemption precisely because nothing
    useful follows from telling those two cases apart.
    """

    family_id: UUID
    client_id: str
    # The access credential this token renews. A rotation revokes it as it
    # mints the next.
    credential_id: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    subject: str | None = None
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VaultCompileRun:
    """One pass of the wiki compiler, from plan to finish.

    The unit compile provenance points at: every wiki document names the run
    that produced it, with ``ON DELETE RESTRICT``, so a run cannot be deleted
    out from under its pages (ADR 0019, migration 0008).

    ``input_frontier`` and ``output_frontier`` record how far the note corpus
    had advanced when the run started and finished. The next run compares
    against the output frontier to find notes written since, which is what
    makes an incremental plan possible without diffing the whole corpus.
    """

    id: UUID
    compiler_principal_id: str
    state: CompileRunState
    started_at: datetime
    completed_at: datetime | None = None
    input_frontier: dict[str, Any] = field(default_factory=dict)
    output_frontier: dict[str, Any] = field(default_factory=dict)
    error_summary: str | None = None


@dataclass(frozen=True, slots=True)
class CompileWorkItem:
    """One page a run should write, and why.

    Deliberately carries note **ids** rather than note bodies. The compiling
    agent fetches what it needs through the ordinary read surface, which is
    already policy-checked (ADR 0014) and already paginated -- a plan that
    inlined bodies would be a second read path with its own disclosure rules,
    and a very large response.
    """

    # None for a page that does not exist yet.
    page_id: str | None
    title: str | None
    # "stale" -- a source moved under an existing page.
    # "missing" -- a source it cites is gone.
    # "new-source" -- a note no page covers.
    reason: str
    source_ids: tuple[str, ...]
