"""Transport-bounds tests for the RunSubmission model (Phase 2).

The action log is opaque at the API boundary — RunSubmission validates only
that `actions` is a bounded list and the envelope fields are well-formed, never
the actions' internal semantics (those belong to a per-scenario validator).
No DB or HTTP client needed; this is pure model validation.
"""
import pytest
from pydantic import ValidationError

from app.models import MAX_RUN_ACTIONS, RunSubmission


def _valid(**overrides) -> dict:
    base = {
        "game_mode": "flood",
        "scenario_version": 1,
        "seed": 123456789,
        "actions": [{"t": 0, "k": "tap"}],
        "claimed_score": 500,
        "client_run_id": "run-abcdef-001",
    }
    base.update(overrides)
    return base


def test_minimal_valid_run_submission():
    run = RunSubmission(**_valid())
    assert run.scenario_version == 1
    assert run.client_run_id == "run-abcdef-001"


def test_claimed_score_optional():
    run = RunSubmission(**_valid(claimed_score=None))
    assert run.claimed_score is None


def test_scenario_version_must_be_positive():
    with pytest.raises(ValidationError):
        RunSubmission(**_valid(scenario_version=0))


def test_client_run_id_min_length_enforced():
    with pytest.raises(ValidationError):
        RunSubmission(**_valid(client_run_id="short"))  # < 8 chars


def test_actions_must_be_a_list():
    with pytest.raises(ValidationError):
        RunSubmission(**_valid(actions={"not": "a list"}))


def test_actions_element_count_capped():
    too_many = [0] * (MAX_RUN_ACTIONS + 1)
    with pytest.raises(ValidationError):
        RunSubmission(**_valid(actions=too_many))


def test_actions_opaque_shape_accepted():
    """Elements may be objects or compact arrays — the model presumes nothing."""
    run = RunSubmission(**_valid(actions=[[0, "tap"], {"t": 1, "k": "hold"}, 42]))
    assert len(run.actions) == 3


def test_claimed_score_non_negative():
    with pytest.raises(ValidationError):
        RunSubmission(**_valid(claimed_score=-1))
