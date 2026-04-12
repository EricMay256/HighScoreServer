import os
import secrets
import pytest
from fastapi.testclient import TestClient


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ["API_KEY"]


@pytest.fixture(scope="module")
def classic_mode(client: TestClient, api_key: str) -> str:
    """Creates a DESC (higher-is-better) game mode. Returns the mode name."""
    response = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "classic", "sort_order": "DESC", "label": "Classic"},
        headers={"x-api-key": api_key},
    )
    assert response.status_code in (200, 201)
    return "classic"


@pytest.fixture(scope="module")
def speedrun_mode(client: TestClient, api_key: str) -> str:
    """Creates an ASC (lower-is-better) game mode. Returns the mode name."""
    response = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "speedrun", "sort_order": "ASC", "label": "Speedrun"},
        headers={"x-api-key": api_key},
    )
    assert response.status_code in (200, 201)
    return "speedrun"


@pytest.fixture(params=["desc", "asc"], ids=["desc_mode", "asc_mode"])
def mode(request, classic_mode, speedrun_mode):
    """
    Direction-agnostic mode fixture for tests that assert invariants
    holding in both sort directions.

    Yields a dict with everything a direction-agnostic test needs:
      - name: the game mode string to send in requests
      - better: a score that should beat any existing record
      - worse: a score that should lose to `better`

    Tests using this fixture run twice — once against a DESC mode where
    "better" means higher, once against an ASC mode where "better" means
    lower. The test body never has to know which direction it's in.

    Reuses the existing classic_mode and speedrun_mode fixtures rather than
    re-seeding game_modes, so the row-creation logic stays in one place.
    """
    if request.param == "desc":
        return {"name": classic_mode, "better": 2000, "worse": 500}
    else:
        return {"name": speedrun_mode, "better": 300, "worse": 500}
    

@pytest.fixture(scope="module")
def requires_auth_mode(client: TestClient, api_key: str) -> str:
    """Creates a game mode that requires a claimed account."""
    response = client.post(
        "/api/leaderboard/game_modes",
        json={"name": "challenge", "sort_order": "DESC", "label": "Challenge", "requires_auth": True},
        headers={"x-api-key": api_key},
    )
    assert response.status_code in (200, 201)
    return "challenge"


@pytest.fixture
def auth_headers(client: TestClient) -> dict:
    """Registers a fresh claimed user and returns Bearer auth headers."""
    suffix = secrets.token_hex(4)
    response = client.post(
        "/api/auth/register",
        json={
            "username": f"testuser_{suffix}",
            "email": f"testuser_{suffix}@example.com",
            "password": "testpassword123",
        },
    )
    assert response.status_code == 201
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def guest_headers(client: TestClient) -> dict:
    """Creates a guest account and returns Bearer auth headers."""
    response = client.post("/api/auth/guest")
    assert response.status_code == 201
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── Happy path ─────────────────────────────────────────────────────────────

def test_submit_score_returns_201(client, auth_headers, classic_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    assert response.status_code == 201


def test_submit_score_response_shape(client, auth_headers, classic_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    data = response.json()
    assert "id" in data
    assert "player" in data
    assert "score" in data
    assert "rank" in data
    assert "percentile" in data
    assert data["score"] == 1000
    assert data["game_mode"] == classic_mode


def test_submit_score_rank_and_percentile_single_player(client, auth_headers, classic_mode):
    """A sole player should be rank 1 at the 100th percentile."""
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    data = response.json()
    assert data["rank"] == 1
    assert data["percentile"] == 100.0


# ── Upsert behavior ────────────────────────────────────────────────────────

def test_better_score_overwrites(client, auth_headers, mode):
    """
    The mirror invariant: an improving submission replaces the stored record.
    Runs against both directions via the `mode` fixture.
    """
    # Seed with a worse score so there's something for the improving
    # submission to overwrite. Without this first POST, the second POST
    # would take the INSERT path of the upsert rather than the UPDATE path,
    # and we'd be testing the wrong thing.
    client.post(
        "/api/leaderboard/scores",
        json={"score": mode["worse"], "game_mode": mode["name"]},
        headers=auth_headers,
    )
    # The submission under test: a better score should replace the seed.
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": mode["better"], "game_mode": mode["name"]},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["score"] == mode["better"]

    # Verify via a separate read path, same as the worse-score test.
    leaderboard = client.get(
        f"/api/leaderboard/scores?game_mode={mode['name']}&period=alltime"
    )
    assert leaderboard.json()["scores"][0]["score"] == mode["better"]


def test_worse_score_does_not_overwrite(client, auth_headers, mode):
    """
    The improvement predicate is the linchpin of the upsert SQL: a submission
    that isn't an improvement must leave the stored record untouched, in either
    sort direction. The `mode` fixture runs this test against both a DESC and
    an ASC mode — the body asserts the invariant without knowing which.
    """
    # Establish the record to defend.
    client.post(
        "/api/leaderboard/scores",
        json={"score": mode["better"], "game_mode": mode["name"]},
        headers=auth_headers,
    )

    # Submit a worse score; the API still returns 201 because the request
    # was valid — the upsert just no-ops on the improvement check.
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": mode["worse"], "game_mode": mode["name"]},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["score"] == mode["better"]

    # Verify the stored record via a separate read path. Without this,
    # a hypothetical bug where submit_score wrote the worse score AND
    # returned the new row would still pass the assertion above.
    leaderboard = client.get(
        f"/api/leaderboard/scores?game_mode={mode['name']}&period=alltime"
    )
    assert leaderboard.json()["scores"][0]["score"] == mode["better"]


# ── Leaderboard ordering ───────────────────────────────────────────────────

def test_leaderboard_desc_ordering(client, classic_mode):
    """Higher scores should appear first on a DESC mode."""
    # Create two users with different scores
    for score, suffix in [(500, "low"), (1500, "high")]:
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"order_test_{suffix}_{secrets.token_hex(3)}",
                "email": f"order_{suffix}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": score, "game_mode": classic_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime")
    assert response.status_code == 200
    scores = response.json()["scores"]
    values = [s["score"] for s in scores]
    assert values == sorted(values, reverse=True)


def test_leaderboard_asc_ordering(client, speedrun_mode):
    """Lower scores should appear first on an ASC mode."""
    for score, suffix in [(300, "fast"), (900, "slow")]:
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"asc_test_{suffix}_{secrets.token_hex(3)}",
                "email": f"asc_{suffix}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": score, "game_mode": speedrun_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(f"/api/leaderboard/scores?game_mode={speedrun_mode}&period=alltime")
    assert response.status_code == 200
    scores = response.json()["scores"]
    values = [s["score"] for s in scores]
    assert values == sorted(values)


# ── Period bucketing ───────────────────────────────────────────────────────

def test_get_scores_valid_periods(client, auth_headers, classic_mode):
    """All three periods should return 200 with the leaderboard envelope."""
    client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    for period in ("alltime", "daily", "weekly"):
        response = client.get(
            f"/api/leaderboard/scores?game_mode={classic_mode}&period={period}"
        )
        assert response.status_code == 200, f"Failed for period: {period}"
        body = response.json()
        assert "scores" in body
        assert "total_count" in body


def test_submit_score_appears_in_all_periods(client, auth_headers, classic_mode):
    """A submitted score should appear in alltime, daily, and weekly buckets."""
    client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    for period in ("alltime", "daily", "weekly"):
        response = client.get(
            f"/api/leaderboard/scores?game_mode={classic_mode}&period={period}"
        )
        assert response.json()["total_count"] == 1, f"Missing score in period: {period}"


# ── Auth gating ────────────────────────────────────────────────────────────

def test_guest_blocked_from_requires_auth_mode(client, guest_headers, requires_auth_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": requires_auth_mode},
        headers=guest_headers,
    )
    assert response.status_code == 403


def test_claimed_user_allowed_in_requires_auth_mode(client, auth_headers, requires_auth_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": requires_auth_mode},
        headers=auth_headers,
    )
    assert response.status_code == 201


def test_invalid_token_returns_401(client, classic_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers={"Authorization": "Bearer this.is.not.a.valid.token"},
    )
    assert response.status_code == 401


def test_expired_token_returns_401(client, classic_mode):
    """A correctly signed but expired token should return 401.
    Depending on FastAPI/Starlette version it could return 403 instead. """
    import os
    from datetime import datetime, timezone, timedelta
    from jose import jwt

    expired_token = jwt.encode(
        {
            "sub": "1",
            "username": "testuser",
            "is_guest": False,
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            "iat": datetime.now(timezone.utc) - timedelta(minutes=61),
        },
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert response.status_code == 401


def test_unauthenticated_request_returns_401(client, classic_mode):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
    )
    assert response.status_code == 401


# ── Error handling ─────────────────────────────────────────────────────────

def test_unknown_game_mode_returns_404(client, auth_headers):
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": "nonexistent_mode"},
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_invalid_period_returns_400(client, classic_mode):
    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=monthly"
    )
    assert response.status_code == 400


def test_unknown_game_mode_on_get_returns_404(client):
    response = client.get(
        "/api/leaderboard/scores?game_mode=nonexistent_mode&period=alltime"
    )
    assert response.status_code == 404


# ── Leaderboard response shape ─────────────────────────────────────────────

def test_get_scores_response_envelope(client, auth_headers, classic_mode):
    """Response should always include scores array and total_count."""
    client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    assert response.status_code == 200
    body = response.json()
    assert "scores" in body
    assert "total_count" in body
    assert isinstance(body["scores"], list)
    assert isinstance(body["total_count"], int)


def test_get_scores_empty_leaderboard(client, classic_mode):
    """An empty leaderboard should return a valid envelope with zero entries."""
    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scores"] == []
    assert body["total_count"] == 0


def test_score_entry_shape(client, auth_headers, classic_mode):
    """Each score entry should include all expected fields."""
    client.post(
        "/api/leaderboard/scores",
        json={"score": 1000, "game_mode": classic_mode},
        headers=auth_headers,
    )
    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    entry = response.json()["scores"][0]
    assert "id" in entry
    assert "player" in entry
    assert "score" in entry
    assert "game_mode" in entry
    assert "submitted_at" in entry
    assert "rank" in entry
    assert "percentile" in entry


# ── Rank and percentile ────────────────────────────────────────────────────

def test_rank_ordering_across_players(client, classic_mode):
    """Rank 1 should have the highest score, rank 2 the next, and so on."""
    for score, suffix in [(1000, "a"), (3000, "b"), (2000, "c")]:
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"rank_test_{suffix}_{secrets.token_hex(3)}",
                "email": f"rank_{suffix}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": score, "game_mode": classic_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    scores = response.json()["scores"]
    assert scores[0]["score"] == 3000
    assert scores[0]["rank"] == 1
    assert scores[0]["percentile"] == 100.0
    assert scores[1]["score"] == 2000
    assert scores[1]["rank"] == 2
    assert scores[1]["percentile"] == 66.67
    assert scores[2]["score"] == 1000
    assert scores[2]["rank"] == 3
    assert scores[2]["percentile"] == 33.33


def test_total_count_matches_player_count(client, classic_mode):
    """total_count should reflect the number of distinct players on the board."""
    for i in range(3):
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"count_test_{i}_{secrets.token_hex(3)}",
                "email": f"count_{i}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": (i + 1) * 100, "game_mode": classic_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    body = response.json()
    assert body["total_count"] == 3
    assert len(body["scores"]) == 3


def test_percentile_first_place_is_100(client, classic_mode):
    """Rank 1 should always be 100th percentile."""
    for score, suffix in [(500, "low"), (1500, "high")]:
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"pct_test_{suffix}_{secrets.token_hex(3)}",
                "email": f"pct_{suffix}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": score, "game_mode": classic_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    scores = response.json()["scores"]
    assert scores[0]["percentile"] == 100.0


def test_percentile_last_place_with_two_players(client, classic_mode):
    """
    With two players, rank 2 percentile should be 50.0.
    Formula: round((1 - (rank - 1) / total) * 100, 2)
    rank=2, total=2 → (1 - 1/2) * 100 = 50.0
    
    This test documents the actual formula behavior rather than asserting
    an intuitive value — worth verifying explicitly since percentile edge
    cases are easy to get wrong.
    """
    for score, suffix in [(500, "low"), (1500, "high")]:
        reg = client.post(
            "/api/auth/register",
            json={
                "username": f"pct2_test_{suffix}_{secrets.token_hex(3)}",
                "email": f"pct2_{suffix}_{secrets.token_hex(3)}@example.com",
                "password": "testpassword123",
            },
        )
        token = reg.json()["access_token"]
        client.post(
            "/api/leaderboard/scores",
            json={"score": score, "game_mode": classic_mode},
            headers={"Authorization": f"Bearer {token}"},
        )

    response = client.get(
        f"/api/leaderboard/scores?game_mode={classic_mode}&period=alltime"
    )
    scores = response.json()["scores"]
    assert scores[1]["percentile"] == 50.0


# ── Game modes endpoint ────────────────────────────────────────────────────

def test_get_game_modes_returns_list(client, classic_mode):
    response = client.get("/api/leaderboard/game_modes")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_game_modes_entry_shape(client, classic_mode):
    """Each game mode entry should include name, sort_order, label, requires_auth."""
    response = client.get("/api/leaderboard/game_modes")
    modes = response.json()
    assert len(modes) >= 1
    entry = next(m for m in modes if m["name"] == classic_mode)
    assert "name" in entry
    assert "sort_order" in entry
    assert "label" in entry
    assert "requires_auth" in entry


def test_get_game_modes_includes_created_mode(client, classic_mode, speedrun_mode):
    """Both created modes should appear in the game modes list."""
    response = client.get("/api/leaderboard/game_modes")
    names = [m["name"] for m in response.json()]
    assert classic_mode in names
    assert speedrun_mode in names

# ── Input Validation ────────────────────────────────────────────────────
def test_score_at_upper_bound_accepted(client, auth_headers, classic_mode):
    """
    The Pydantic cap is le=18_000_000_420. Exactly the cap must be accepted —
    this is the contract the C# client's score field needs to honor.
    """
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 18_000_000_420, "game_mode": classic_mode},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["score"] == 18_000_000_420


def test_score_over_upper_bound_rejected(client, auth_headers, classic_mode):
    """One over the cap must be rejected by Pydantic with 422."""
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 18_000_000_421, "game_mode": classic_mode},
        headers=auth_headers,
    )
    assert response.status_code == 422