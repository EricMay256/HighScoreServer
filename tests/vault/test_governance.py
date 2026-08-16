"""The ported decision policy (vault ADR 0004).

These mirror the source's own cases so the port stays diffable. The band tests
exercise branches that are *dormant* under the shipped policy — reject, merge,
and link are all disabled — which is exactly why they are here: ADR 0004 keeps
them normative for a later stage, and a dormant branch nobody tests is a branch
that will be wrong when it wakes up.
"""

import pytest

from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.governance import (
    DEFAULT_POLICY,
    Flag,
    Insert,
    Link,
    Merge,
    Policy,
    Reject,
    ScoredCandidate,
    decide,
    validate,
)


def candidate(**overrides) -> NewVaultDocument:
    base = {
        "id": "candidate",
        "kind": DocumentKind.NOTE,
        "vault_path": "Agent/notes/candidate.md",
        "status": DocumentStatus.ACTIVE,
        "title": "Two-phase tick updates",
        "body": "Read into a buffer, then commit.",
        "contributed_by": "agent:test",
        "provenance": {},
    }
    return NewVaultDocument(**{**base, **overrides})


def scored(score: float, note_id: str = "existing") -> ScoredCandidate:
    return ScoredCandidate(note_id=note_id, title="Existing note", score=score)


def test_no_similars_inserts() -> None:
    assert isinstance(decide(candidate(), [], Policy(flag_at=0.85)), Insert)


def test_a_score_below_every_band_inserts() -> None:
    action = decide(candidate(), [scored(0.5)], Policy(flag_at=0.85))

    assert isinstance(action, Insert)


def test_a_score_at_the_flag_band_flags() -> None:
    """The band is inclusive: `>= flag_at`, not `>`."""

    action = decide(candidate(), [scored(0.85)], Policy(flag_at=0.85))

    assert isinstance(action, Flag)
    assert "possible duplicate of existing" in action.reason


def test_the_highest_scoring_similar_decides() -> None:
    """Not the first, and not the last — the maximum."""

    action = decide(
        candidate(),
        [scored(0.1, "low"), scored(0.9, "high"), scored(0.4, "mid")],
        Policy(flag_at=0.85),
    )

    assert isinstance(action, Flag)
    assert "high" in action.reason


def test_bands_are_evaluated_strongest_first() -> None:
    """A score above several bands takes the strongest, not the first set."""

    policy = Policy(flag_at=0.85, merge_at=0.9, reject_at=0.95, link_at=0.7)

    assert isinstance(decide(candidate(), [scored(0.99)], policy), Reject)
    assert isinstance(decide(candidate(), [scored(0.92)], policy), Merge)
    assert isinstance(decide(candidate(), [scored(0.87)], policy), Flag)
    assert isinstance(decide(candidate(), [scored(0.75)], policy), Link)
    assert isinstance(decide(candidate(), [scored(0.5)], policy), Insert)


def test_link_collects_every_candidate_above_the_link_band() -> None:
    action = decide(
        candidate(),
        [scored(0.8, "a"), scored(0.75, "b"), scored(0.6, "c")],
        Policy(flag_at=0.85, link_at=0.7),
    )

    assert isinstance(action, Link)
    assert action.related_ids == ["a", "b"]


def test_disabled_bands_are_skipped() -> None:
    """A None band must not be treated as zero, or everything would reject."""

    action = decide(candidate(), [scored(0.99)], Policy(flag_at=1.0))

    assert isinstance(action, Insert)


@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"flag_at": 0.9, "reject_at": 0.8},
        {"flag_at": 0.9, "merge_at": 0.85},
        {"flag_at": 0.5, "link_at": 0.6},
    ],
)
def test_out_of_order_bands_are_rejected(policy_kwargs: dict) -> None:
    with pytest.raises(ValueError, match="out of order"):
        Policy(**policy_kwargs)


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_bands_outside_zero_to_one_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="must be in"):
        Policy(flag_at=value)


def test_the_shipped_policy_flags_only_an_identical_embedding() -> None:
    """Pins the calibration decision, not just the number.

    Stage A's 0.85 is a normalized-title string ratio; here the score is cosine
    similarity, where unrelated prose routinely exceeds 0.7. Porting the logic
    verbatim and porting a calibrated constant verbatim are different acts, so
    the shipped policy flags only an exact match until a threshold is measured
    against the real corpus.
    """

    assert DEFAULT_POLICY.flag_at == 1.0
    assert DEFAULT_POLICY.reject_at is None
    assert DEFAULT_POLICY.merge_at is None
    assert DEFAULT_POLICY.link_at is None

    # A very close but non-identical neighbour still inserts.
    assert isinstance(decide(candidate(), [scored(0.97)], DEFAULT_POLICY), Insert)
    # An identical embedding does not.
    assert isinstance(decide(candidate(), [scored(1.0)], DEFAULT_POLICY), Flag)


def test_validate_accepts_a_well_formed_candidate() -> None:
    assert validate(candidate()) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"title": "   "}, "title must not be empty"),
        ({"body": "  \n "}, "body must not be empty"),
        ({"contributed_by": " "}, "contributed_by must be set"),
    ],
)
def test_validate_reports_missing_required_fields(
    overrides: dict, expected: str
) -> None:
    errors = validate(candidate(**overrides))

    assert any(expected in error for error in errors)


def test_validate_rejects_blank_and_duplicate_tags() -> None:
    blank = validate(candidate(tags=("ok", "  ")))
    duplicate = validate(candidate(tags=("same", "same")))

    assert any("invalid tag" in error for error in blank)
    assert any("duplicate tags" in error for error in duplicate)
