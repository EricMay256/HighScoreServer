"""Unit tests for the tiered validator (Phase 3). Pure logic, no DB/HTTP.

Tier 1 is concrete; tier 2's recompute mechanism is exercised with an injected
fake scorer (no real scenario scorer exists in-repo yet); tier 3 is a deferred
stub that must reject.
"""
from app.models import MAX_SCORE
from app.validation import RunRecord, TieredValidator


def _run(**overrides) -> RunRecord:
    base = dict(
        id=1, user_id=1, game_mode="m", scenario_version=1, seed=7,
        actions=[{"a": 1}], claimed_score=100,
    )
    base.update(overrides)
    return RunRecord(**base)


# ── Tier 1: bounds/shape, claim becomes canonical ────────────────────────────

def test_tier1_validates_plausible_claim():
    result = TieredValidator().validate(_run(claimed_score=500), 1)
    assert result.status == "validated"
    assert result.canonical_score == 500   # the plausible claim is canonical
    assert result.tier_achieved == 1


def test_tier1_rejects_empty_action_log():
    result = TieredValidator().validate(_run(actions=[]), 1)
    assert result.status == "rejected"
    assert "empty action log" in result.reason


def test_tier1_rejects_missing_claim():
    result = TieredValidator().validate(_run(claimed_score=None), 1)
    assert result.status == "rejected"


def test_tier1_rejects_claim_out_of_bounds():
    result = TieredValidator().validate(_run(claimed_score=MAX_SCORE + 1), 1)
    assert result.status == "rejected"


# ── Tier 2: recompute via injected scorer; claim recorded, not trusted ───────

def test_tier2_recomputes_from_action_log():
    scorer = lambda run: sum(a.get("pts", 0) for a in run.actions)  # noqa: E731
    v = TieredValidator({1: scorer})
    result = v.validate(
        _run(scenario_version=1, actions=[{"pts": 10}, {"pts": 5}], claimed_score=999),
        2,
    )
    assert result.status == "validated"
    assert result.canonical_score == 15   # recomputed, not the claimed 999
    assert result.tier_achieved == 2


def test_tier2_claim_mismatch_is_not_a_rejection():
    """A wildly different claim is recorded but the recomputed value wins."""
    v = TieredValidator({1: lambda run: 42})
    result = v.validate(_run(scenario_version=1, claimed_score=999_999), 2)
    assert result.status == "validated"
    assert result.canonical_score == 42


def test_tier2_rejects_when_no_scorer_registered():
    result = TieredValidator().validate(_run(scenario_version=7), 2)
    assert result.status == "rejected"
    assert "no tier-2 scorer" in result.reason


def test_tier2_scorer_exception_becomes_rejection_not_crash():
    def boom(run):
        raise ValueError("malformed log")
    result = TieredValidator({1: boom}).validate(_run(scenario_version=1), 2)
    assert result.status == "rejected"
    assert "recompute failed" in result.reason


# ── Tier 3: deferred stub ────────────────────────────────────────────────────

def test_tier3_is_a_deferred_stub_that_rejects():
    result = TieredValidator().validate(_run(), 3)
    assert result.status == "rejected"
    assert "tier-3" in result.reason.lower()
