"""Phase 2: cumulative scoring, idempotency dedup, and the surfaced
game_mode/validation response fields.

Cumulative modes accumulate an increment per submission (deduped by
idempotency key) instead of keeping a personal best. These tests exercise the
shared write tail through the /scores endpoint.
"""
import os
import secrets

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ["API_KEY"]


@pytest.fixture(scope="module")
def cumulative_mode(client: TestClient, api_key: str) -> str:
    """A cumulative DESC mode tagged with a game_key."""
    response = client.post(
        "/api/leaderboard/game_modes",
        json={
            "name": "coins",
            "sort_order": "DESC",
            "label": "Coins Collected",
            "scoring_strategy": "cumulative",
            "game_key": "flick_fest",
        },
        headers={"x-api-key": api_key},
    )
    assert response.status_code in (200, 201)
    return "coins"


@pytest.fixture(scope="module")
def best_mode(client: TestClient, api_key: str) -> str:
    """A plain best-wins mode created with no new fields (defaults apply)."""
    response = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "arcade", "sort_order": "DESC", "label": "Arcade"},
        headers={"x-api-key": api_key},
    )
    assert response.status_code in (200, 201)
    return "arcade"


@pytest.fixture
def auth_headers(client: TestClient) -> dict:
    suffix = secrets.token_hex(4)
    response = client.post(
        "/api/auth/register",
        json={
            "username": f"cum_{suffix}",
            "email": f"cum_{suffix}@example.com",
            "password": "testpassword123",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _submit(client, headers, mode, score, key=None):
    body = {"score": score, "game_mode": mode}
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/api/leaderboard/scores", json=body, headers=headers)


# ── game_mode config surfacing ───────────────────────────────────────────────

def test_cumulative_mode_config_round_trips(client, cumulative_mode):
    modes = client.get("/api/leaderboard/game_modes").json()
    entry = next(m for m in modes if m["name"] == cumulative_mode)
    assert entry["scoring_strategy"] == "cumulative"
    assert entry["required_tier"] == 0
    assert entry["game_key"] == "flick_fest"


def test_default_mode_has_safe_field_defaults(client, best_mode):
    modes = client.get("/api/leaderboard/game_modes").json()
    entry = next(m for m in modes if m["name"] == best_mode)
    assert entry["scoring_strategy"] == "best"
    assert entry["required_tier"] == 0
    assert entry["game_key"] is None


# ── validation fields on the response ─────────────────────────────────────────

def test_score_response_carries_validation_fields(client, auth_headers, best_mode):
    """Raw submissions report validated=false / validation_tier=0."""
    data = _submit(client, auth_headers, best_mode, 1000).json()
    assert data["validated"] is False
    assert data["validation_tier"] == 0


# ── cumulative accumulation ───────────────────────────────────────────────────

def test_cumulative_sums_distinct_increments(client, auth_headers, cumulative_mode):
    first = _submit(client, auth_headers, cumulative_mode, 100, key="key-aaaa-1")
    assert first.status_code == 201
    assert first.json()["score"] == 100

    second = _submit(client, auth_headers, cumulative_mode, 50, key="key-bbbb-2")
    assert second.status_code == 201
    assert second.json()["score"] == 150


def test_cumulative_dedupes_repeated_key(client, auth_headers, cumulative_mode):
    """A repeated idempotency key is a no-op — the increment is not re-applied."""
    _submit(client, auth_headers, cumulative_mode, 100, key="dup-key-123")
    repeat = _submit(client, auth_headers, cumulative_mode, 100, key="dup-key-123")
    assert repeat.status_code == 201
    assert repeat.json()["score"] == 100  # unchanged, not 200

    # Confirm via a separate read path.
    board = client.get(
        f"/api/leaderboard/scores?game_mode={cumulative_mode}&period=alltime"
    ).json()
    assert board["scores"][0]["score"] == 100


def test_cumulative_accumulates_per_period(client, auth_headers, cumulative_mode):
    """Each period bucket accumulates the same increments independently."""
    _submit(client, auth_headers, cumulative_mode, 30, key="per-key-1")
    _submit(client, auth_headers, cumulative_mode, 70, key="per-key-2")
    for period in ("alltime", "daily", "weekly"):
        board = client.get(
            f"/api/leaderboard/scores?game_mode={cumulative_mode}&period={period}"
        ).json()
        assert board["scores"][0]["score"] == 100, f"period {period}"


def test_cumulative_requires_idempotency_key(client, auth_headers, cumulative_mode):
    """Submitting to a cumulative mode without a key is a 422."""
    resp = _submit(client, auth_headers, cumulative_mode, 100)  # no key
    assert resp.status_code == 422


# ── best modes are unaffected ─────────────────────────────────────────────────

def test_best_mode_keeps_personal_best_not_sum(client, auth_headers, best_mode):
    """A best mode still keeps the max, and ignores any idempotency key sent."""
    _submit(client, auth_headers, best_mode, 100, key="ignored-key-1")
    _submit(client, auth_headers, best_mode, 50, key="ignored-key-2")
    board = client.get(
        f"/api/leaderboard/scores?game_mode={best_mode}&period=alltime"
    ).json()
    assert board["scores"][0]["score"] == 100  # best, not 150
