"""Phase 3: the /runs endpoint, cross-routing 409s, validation, idempotency.

Run-required modes (required_tier >= 1) validate a full run and upsert the
server-computed canonical score; raw modes reject runs. Tier-1 validation is
what the live default validator can do today, so these exercise that path.
"""
import os
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ["API_KEY"]


@pytest.fixture(scope="module")
def run_mode(client: TestClient, api_key: str) -> str:
    """A run-required (tier 1) best mode."""
    resp = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "validated", "sort_order": "DESC", "label": "Validated",
              "required_tier": 1},
        headers={"x-api-key": api_key},
    )
    assert resp.status_code in (200, 201)
    assert resp.json()["required_tier"] == 1
    return "validated"


@pytest.fixture(scope="module")
def raw_mode(client: TestClient, api_key: str) -> str:
    """A plain raw (tier 0) mode."""
    resp = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "rawmode", "sort_order": "DESC", "label": "Raw"},
        headers={"x-api-key": api_key},
    )
    assert resp.status_code in (200, 201)
    return "rawmode"


@pytest.fixture
def auth_headers(client: TestClient) -> dict:
    suffix = secrets.token_hex(4)
    resp = client.post(
        "/api/auth/register",
        json={"username": f"run_{suffix}", "email": f"run_{suffix}@example.com",
              "password": "testpassword123"},
    )
    assert resp.status_code == 201
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _run_body(**overrides) -> dict:
    body = {
        "game_mode": "validated",
        "scenario_version": 1,
        "seed": 123456789,
        "actions": [{"t": 0, "k": "tap"}, {"t": 1, "k": "tap"}],
        "claimed_score": 1234,
        "client_run_id": f"run-{secrets.token_hex(6)}",
    }
    body.update(overrides)
    return body


def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


# ── Cross-routing 409s ───────────────────────────────────────────────────────

def test_raw_score_to_run_required_mode_returns_guided_409(client, auth_headers, run_mode):
    resp = client.post(
        "/api/leaderboard/scores",
        json={"score": 100, "game_mode": run_mode},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert isinstance(body["detail"], str)            # human-readable string
    assert body["code"] == "RUN_REQUIRED"             # sibling, not nested
    assert body["submit_to"] == "/api/leaderboard/runs"


def test_run_to_raw_mode_returns_guided_409(client, auth_headers, raw_mode):
    resp = client.post(
        "/api/leaderboard/runs",
        json=_run_body(game_mode=raw_mode),
        headers=auth_headers,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert isinstance(body["detail"], str)
    assert body["code"] == "RAW_ONLY"
    assert body["submit_to"] == "/api/leaderboard/scores"


# ── Validated run pipeline ───────────────────────────────────────────────────

def test_validated_run_writes_canonical_score(client, auth_headers, run_mode):
    body = _run_body(claimed_score=1234)
    resp = client.post("/api/leaderboard/runs", json=body, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["score"] == 1234
    assert data["validated"] is True
    assert data["validation_tier"] == 1

    # The score is on the leaderboard via a separate read path.
    board = client.get(f"/api/leaderboard/scores?game_mode={run_mode}&period=alltime").json()
    assert board["scores"][0]["score"] == 1234

    # And the run row is validated with the canonical score and links the score.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, canonical_score FROM runs WHERE client_run_id = %s",
                (body["client_run_id"],),
            )
            status, canonical = cur.fetchone()
            assert status == "validated"
            assert canonical == 1234
            cur.execute(
                "SELECT run_id FROM scores WHERE game_mode = %s AND period = 'alltime'",
                (run_mode,),
            )
            assert cur.fetchone()[0] is not None  # score linked to a run
    finally:
        conn.close()


def test_claimed_score_is_recorded_but_canonical_is_server_set(client, auth_headers, run_mode):
    """At tier 1 the plausible claim becomes canonical; both are stored on the run."""
    body = _run_body(claimed_score=777)
    client.post("/api/leaderboard/runs", json=body, headers=auth_headers)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT claimed_score, canonical_score FROM runs WHERE client_run_id = %s",
                (body["client_run_id"],),
            )
            claimed, canonical = cur.fetchone()
            assert claimed == 777
            assert canonical == 777
    finally:
        conn.close()


def test_run_with_empty_actions_is_rejected_422(client, auth_headers, run_mode):
    body = _run_body(actions=[])
    resp = client.post("/api/leaderboard/runs", json=body, headers=auth_headers)
    assert resp.status_code == 422
    assert "rejected" in resp.json()["detail"].lower()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM runs WHERE client_run_id = %s",
                (body["client_run_id"],),
            )
            assert cur.fetchone()[0] == "rejected"
    finally:
        conn.close()


# ── Idempotency / anti-replay ────────────────────────────────────────────────

def test_duplicate_client_run_id_returns_prior_result(client, auth_headers, run_mode):
    body = _run_body(claimed_score=2000)
    first = client.post("/api/leaderboard/runs", json=body, headers=auth_headers)
    assert first.status_code == 201

    # Resubmit the exact same client_run_id (even with a different claimed_score):
    # it returns the prior result without re-validating or writing a new run.
    replay = client.post(
        "/api/leaderboard/runs",
        json={**body, "claimed_score": 99999},
        headers=auth_headers,
    )
    assert replay.status_code == 201
    assert replay.json()["score"] == 2000   # prior result, not the replayed claim
    assert replay.json()["validated"] is True

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM runs WHERE client_run_id = %s",
                (body["client_run_id"],),
            )
            assert cur.fetchone()[0] == 1   # no second run row created
    finally:
        conn.close()


# ── Read-path enrichment ─────────────────────────────────────────────────────

def test_get_scores_surfaces_validation_for_validated_run(client, auth_headers, run_mode):
    """A leaderboard row produced by a validated run reads back validated."""
    client.post("/api/leaderboard/runs", json=_run_body(claimed_score=555), headers=auth_headers)
    board = client.get(f"/api/leaderboard/scores?game_mode={run_mode}&period=alltime").json()
    entry = board["scores"][0]
    assert entry["validated"] is True
    assert entry["validation_tier"] == 1


def test_latest_surfaces_validation_for_validated_run(client, auth_headers, run_mode):
    """/latest reflects validation status too (same join, separate query)."""
    client.post("/api/leaderboard/runs", json=_run_body(claimed_score=321), headers=auth_headers)
    latest = client.get(f"/api/leaderboard/latest?game_modes={run_mode}").json()
    entry = next(e for e in latest["scores"] if e["game_mode"] == run_mode)
    assert entry["validated"] is True
    assert entry["validation_tier"] == 1


def test_get_scores_unvalidated_for_raw_score(client, auth_headers, raw_mode):
    """A raw (run_id NULL) leaderboard row reads back unvalidated."""
    client.post(
        "/api/leaderboard/scores",
        json={"score": 100, "game_mode": raw_mode},
        headers=auth_headers,
    )
    board = client.get(f"/api/leaderboard/scores?game_mode={raw_mode}&period=alltime").json()
    entry = board["scores"][0]
    assert entry["validated"] is False
    assert entry["validation_tier"] == 0
