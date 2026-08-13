"""Contribution validation and the dedup decision policy.

Ported from `vault_contrib.core` and `vault_contrib.models` in the private
knowledge-platform engine. Vault ADR 0004 makes those normative: the band logic
below is the vault's curation policy and is deliberately transcribed rather
than reinterpreted, so a reviewer can diff it against the source.

What is deliberately *not* transcribed is the **value** of `flag_at`. Stage A's
0.85 is a normalized-title string ratio; here the score is cosine similarity on
an embedding model. Those are different scales measured over different things,
so the number does not carry across: porting logic verbatim and porting a
calibrated constant verbatim are not the same act. A `flag_at` is derived per
model by the two-sided procedure in ``calibration.py`` and
``docs/embedding-calibration.md``. See ``DEFAULT_POLICY``.

This module is pure: no I/O, no database, no embedding calls. That is what makes
it testable against the source's own test cases.
"""

from dataclasses import dataclass, field

from .domain import NewVaultDocument


# The candidate is passed through the decision untouched, so the policy has no
# opinion about what a document is — exactly as in the source, where `decide`
# neither knows nor cares whether the scores came from string matching or
# cosine distance.
Candidate = NewVaultDocument

MIN_BODY_CHARS = 1


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """An existing document the deduper surfaced, with its similarity score."""

    note_id: str
    title: str
    score: float


@dataclass(frozen=True, slots=True)
class Insert:
    note: Candidate


@dataclass(frozen=True, slots=True)
class Flag:
    note: Candidate
    reason: str
    similars: list[ScoredCandidate]


@dataclass(frozen=True, slots=True)
class Link:
    note: Candidate
    related_ids: list[str]


@dataclass(frozen=True, slots=True)
class Merge:
    into_id: str
    note: Candidate


@dataclass(frozen=True, slots=True)
class Reject:
    reason: str
    conflicting_id: str


Action = Insert | Flag | Link | Merge | Reject


@dataclass(frozen=True, slots=True)
class Policy:
    """Similarity thresholds -> action. This IS the vault's curation policy.

    Bands are checked high score -> low. A ``None`` band is disabled. The A
    stage sets only ``flag_at``; the others stay None and activate for free
    once a semantic deduper produces meaningful mid-range scores.

    Required ordering when set: reject_at >= merge_at >= flag_at >= link_at.
    """

    flag_at: float = 1.0
    reject_at: float | None = None
    merge_at: float | None = None
    link_at: float | None = None

    def __post_init__(self) -> None:
        bands = [
            ("reject_at", self.reject_at),
            ("merge_at", self.merge_at),
            ("flag_at", self.flag_at),
            ("link_at", self.link_at),
        ]
        present = [(n, v) for n, v in bands if v is not None]
        for (n1, v1), (n2, v2) in zip(present, present[1:], strict=False):
            if v1 < v2:
                raise ValueError(
                    f"Policy bands out of order: {n1}={v1} must be >= {n2}={v2}"
                )
        for n, v in present:
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"Policy.{n}={v} must be in [0, 1]")


# `flag_at = 1.0` means only an *identical* embedding flags. That is not the
# same as switching dedup off: byte-identical text produces the same vector and
# a cosine similarity of 1.0, so exact resubmission is still caught. It is dedup
# narrowed to the one band that needs no calibration.
#
# 1.0 is the correct default for any model whose distribution has not been
# measured, and it is also — as of 2026-08-12 — the *measured* answer for
# `text-embedding-3-small`. The corpus's closest legitimately-distinct pair
# scores 0.7406 and the weakest deliberate restatement scores 0.7500: a gap of
# 0.0094, which is sampling noise. This model does not separate restatement from
# adjacency on a corpus of short operational notes, so there is no threshold
# below 1.0 that is not wrong in both directions at once.
#
# Do not set this from a literature constant, from the corpus distribution
# alone, or by eye. `docs/embedding-calibration.md` carries the procedure and
# the per-model register; a change here needs a new row in it.
DEFAULT_POLICY = Policy()


def validate(note: Candidate) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    The HTTP boundary already rejects most of this through Pydantic. Keeping
    the ported check as well is deliberate: it is the normative artifact, it
    guards callers that are not the HTTP route (the importer may grow one), and
    it keeps this module diffable against its source.
    """

    errors: list[str] = []
    if not note.title.strip():
        errors.append("title must not be empty")
    if len(note.body.strip()) < MIN_BODY_CHARS:
        errors.append("body must not be empty")
    if not note.contributed_by.strip():
        errors.append("contributed_by must be set (which agent is writing?)")
    for tag in note.tags:
        if not isinstance(tag, str) or not tag.strip():
            errors.append(f"invalid tag: {tag!r} (tags must be non-empty strings)")
    if len(note.tags) != len(set(note.tags)):
        errors.append("duplicate tags are not allowed")
    return errors


def decide(
    candidate: Candidate,
    similars: list[ScoredCandidate],
    policy: Policy,
) -> Action:
    """Map the top similarity score to an Action via the policy bands.

    Bands are evaluated strongest-match-first. Disabled (None) bands are
    skipped, so a policy that sets only ``flag_at`` collapses to: exact-ish
    duplicate -> Flag, otherwise -> Insert. Setting merge_at / link_at lights
    up those branches with no change here.
    """

    if not similars:
        return Insert(note=candidate)

    top = max(similars, key=lambda c: c.score)
    s = top.score

    if policy.reject_at is not None and s >= policy.reject_at:
        return Reject(
            reason=f"near-identical to existing note (score={s:.3f})",
            conflicting_id=top.note_id,
        )

    if policy.merge_at is not None and s >= policy.merge_at:
        return Merge(into_id=top.note_id, note=candidate)

    if s >= policy.flag_at:
        return Flag(
            note=candidate,
            reason=f"possible duplicate of {top.note_id} (score={s:.3f})",
            similars=similars,
        )

    if policy.link_at is not None and s >= policy.link_at:
        related = [c.note_id for c in similars if c.score >= policy.link_at]
        return Link(note=candidate, related_ids=related)

    return Insert(note=candidate)


@dataclass(frozen=True, slots=True)
class ContributionOutcome:
    """Structured result handed back to the caller."""

    status: str  # inserted | flagged | linked | rejected | invalid
    note_id: str | None
    message: str
    similars: list[ScoredCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    idempotent_replay: bool = False
