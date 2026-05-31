"""Run validation seam.

Defines the Validator interface and a tiered default implementation. The seam
isolates the rest of the system from the (deferred) deterministic replay core:
tiers 1-2 are real here; tier 3 exists as a marked integration point only — its
binding (in-process port / subprocess / sidecar) and the replay core itself are
intentionally undecided (see specs.md, "OPEN - tier-3 binding").

The mode's required_tier is the *minimum*; the validator achieves at least that
and records the tier actually achieved, so old runs can be re-validated at a
higher tier later with no schema change.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel

from app.models import MAX_RUN_ACTIONS, MAX_SCORE


class RunRecord(BaseModel):
    """What the validator sees: the persisted run plus its decompressed actions."""
    id: int
    user_id: int
    game_mode: str
    scenario_version: int
    seed: int
    actions: list[Any]
    claimed_score: int | None


class ValidationResult(BaseModel):
    canonical_score: int
    tier_achieved: int
    status: Literal["validated", "rejected"]
    reason: str | None = None


class Validator(Protocol):
    def validate(self, run: RunRecord, required_tier: int) -> ValidationResult: ...


# A tier-2 scorer recomputes the canonical score from a run's action log for a
# specific scenario_version. None are registered in-repo: the action shape and
# scoring rules are pinned per scenario when a real run producer lands (see
# specs.md "Deferred: typed action shape"). The registry is the seam; tests
# inject a fake to exercise the recompute path.
ScenarioScorer = Callable[["RunRecord"], int]


class TieredValidator:
    """Validates a run to (at least) the mode's required tier.

    tier 1 - bounds/shape only, no sim: the claimed score is accepted as
             canonical if it is plausible. The weakest tier.
    tier 2 - score recompute: a per-scenario scorer re-derives the canonical
             score from the action log. The claim is recorded but never trusted;
             a mismatch does not reject (the recomputed value wins).
    tier 3 - deterministic replay: DEFERRED. Exists as an integration point only.
    """

    def __init__(self, tier2_scorers: dict[int, ScenarioScorer] | None = None) -> None:
        self._tier2_scorers = dict(tier2_scorers or {})

    def validate(self, run: RunRecord, required_tier: int) -> ValidationResult:
        if required_tier <= 1:
            return self._tier1(run)
        if required_tier == 2:
            return self._tier2(run)
        if required_tier == 3:
            return self._tier3(run)
        return ValidationResult(
            canonical_score=0, tier_achieved=0, status="rejected",
            reason=f"unsupported required_tier {required_tier}",
        )

    def _tier1(self, run: RunRecord) -> ValidationResult:
        reason = self._bounds_reason(run, require_claim=True)
        if reason is not None:
            return ValidationResult(
                canonical_score=0, tier_achieved=1, status="rejected", reason=reason
            )
        # No recompute at tier 1 — the plausible claim becomes canonical.
        return ValidationResult(
            canonical_score=run.claimed_score, tier_achieved=1, status="validated"
        )

    def _tier2(self, run: RunRecord) -> ValidationResult:
        reason = self._bounds_reason(run, require_claim=False)
        if reason is not None:
            return ValidationResult(
                canonical_score=0, tier_achieved=2, status="rejected", reason=reason
            )
        scorer = self._tier2_scorers.get(run.scenario_version)
        if scorer is None:
            return ValidationResult(
                canonical_score=0, tier_achieved=2, status="rejected",
                reason=f"no tier-2 scorer registered for scenario_version {run.scenario_version}",
            )
        try:
            canonical = int(scorer(run))
        except Exception as e:  # a malformed action log is a rejection, not a 500
            return ValidationResult(
                canonical_score=0, tier_achieved=2, status="rejected",
                reason=f"tier-2 recompute failed: {e}",
            )
        # The claim is recorded on the runs row but not trusted; a mismatch is
        # not a rejection — the recomputed value is authoritative.
        return ValidationResult(
            canonical_score=canonical, tier_achieved=2, status="validated"
        )

    def _tier3(self, run: RunRecord) -> ValidationResult:
        # DEFERRED — tier 3 exists as a concept and an integration point only.
        # The binding (in-process port / subprocess / sidecar) and the replay
        # core are intentionally not decided here (see specs.md OPEN tier-3).
        # A real Tier3Validator plugs in at this seam.
        return ValidationResult(
            canonical_score=0, tier_achieved=0, status="rejected",
            reason="tier-3 deterministic replay is not yet available",
        )

    @staticmethod
    def _bounds_reason(run: RunRecord, *, require_claim: bool) -> str | None:
        """Scenario-agnostic plausibility checks; returns a rejection reason or None.

        Deliberately generic: per-scenario score ceilings and duration
        plausibility need the (deferred) action shape, so only universal bounds
        are enforced here.
        """
        if not run.actions:
            return "empty action log"
        if len(run.actions) > MAX_RUN_ACTIONS:
            return f"action log exceeds {MAX_RUN_ACTIONS} elements"
        if require_claim:
            if run.claimed_score is None:
                return "claimed_score required for tier-1 validation"
            if not 0 <= run.claimed_score <= MAX_SCORE:
                return "claimed_score out of bounds"
        return None


# Default instance used by the endpoint. No tier-2 scorers are registered, so a
# tier-2 mode rejects until a real scenario scorer is added; tier-1 modes
# validate today.
default_validator: Validator = TieredValidator()
