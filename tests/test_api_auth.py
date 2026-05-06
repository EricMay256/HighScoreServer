import os
import secrets
from jose import jwt
from fastapi.testclient import TestClient


# ── Helpers ────────────────────────────────────────────────────────────────

def decode_token(token: str) -> dict:
    """Decode a JWT without verifying expiry — for inspecting payload in tests."""
    return jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])


def random_user() -> dict:
    """Generates a unique user payload for registration."""
    suffix = secrets.token_hex(4)
    return {
        "username": f"user_{suffix}",
        "email": f"user_{suffix}@example.com",
        "password": "testpassword123",
    }


def register(client: TestClient, user: dict | None = None) -> dict:
    """Registers a user and returns the full response JSON."""
    user = user or random_user()
    response = client.post("/api/auth/register", json=user)
    assert response.status_code == 201
    return response.json()


def guest(client: TestClient) -> dict:
    """Creates a guest account and returns the full response JSON."""
    response = client.post("/api/auth/guest")
    assert response.status_code == 201
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── register ───────────────────────────────────────────────────────────────

def test_register_returns_201(client):
    user = random_user()
    response = client.post("/api/auth/register", json=user)
    assert response.status_code == 201


def test_register_returns_tokens(client):
    tokens = register(client)
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"


def test_register_token_payload(client):
    user = random_user()
    tokens = register(client, user)
    payload = decode_token(tokens["access_token"])
    assert payload["username"] == user["username"]
    assert payload["is_guest"] is False


def test_register_duplicate_username_returns_409(client):
    user = random_user()
    register(client, user)
    dupe = {**user, "email": f"other_{secrets.token_hex(4)}@example.com"}
    response = client.post("/api/auth/register", json=dupe)
    assert response.status_code == 409


def test_register_duplicate_email_returns_409(client):
    user = random_user()
    register(client, user)
    dupe = {**user, "username": f"other_{secrets.token_hex(4)}"}
    response = client.post("/api/auth/register", json=dupe)
    assert response.status_code == 409


def test_register_short_username_returns_422(client):
    user = {**random_user(), "username": "ab"}
    response = client.post("/api/auth/register", json=user)
    assert response.status_code == 422


def test_register_short_password_returns_422(client):
    user = {**random_user(), "password": "short"}
    response = client.post("/api/auth/register", json=user)
    assert response.status_code == 422


# ── login ──────────────────────────────────────────────────────────────────

def test_login_happy_path(client):
    user = random_user()
    register(client, user)
    response = client.post(
        "/api/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert response.status_code == 200
    assert "access_token" in response.json()
    assert "refresh_token" in response.json()


def test_login_wrong_password_returns_401(client):
    user = random_user()
    register(client, user)
    response = client.post(
        "/api/auth/login",
        json={"username": user["username"], "password": "wrongpassword"},
    )
    assert response.status_code == 401


def test_login_unknown_username_returns_401(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "nobody_real", "password": "testpassword123"},
    )
    assert response.status_code == 401


# ── refresh ────────────────────────────────────────────────────────────────

def test_refresh_returns_new_tokens(client):
    tokens = register(client)
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 200
    new_tokens = response.json()
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens


def test_refresh_rotates_token(client):
    """The original refresh token should be invalidated after use."""
    tokens = register(client)
    client.post(
        "/api/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    # Attempt to reuse the original token
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 401


def test_refresh_new_token_is_usable(client):
    """The new refresh token returned from rotation should work."""
    tokens = register(client)
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    new_tokens = response.json()
    response2 = client.post(
        "/api/auth/refresh",
        json={"refresh_token": new_tokens["refresh_token"]},
    )
    assert response2.status_code == 200


def test_refresh_invalid_token_returns_401(client):
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": "not-a-real-token"},
    )
    assert response.status_code == 401


# ── logout ─────────────────────────────────────────────────────────────────

def test_logout_returns_204(client):
    tokens = register(client)
    response = client.post(
        "/api/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 204


def test_logout_invalidates_refresh_token(client):
    """After logout, the refresh token should no longer be usable."""
    tokens = register(client)
    client.post(
        "/api/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 401


# ── guest ──────────────────────────────────────────────────────────────────

def test_guest_returns_201(client):
    response = client.post("/api/auth/guest")
    assert response.status_code == 201


def test_guest_returns_tokens(client):
    tokens = guest(client)
    assert "access_token" in tokens
    assert "refresh_token" in tokens


def test_guest_token_payload(client):
    tokens = guest(client)
    payload = decode_token(tokens["access_token"])
    assert payload["is_guest"] is True
    assert payload["username"].startswith("guest_")


def test_guest_token_is_valid_for_authenticated_routes(client):
    """Guest tokens should work on routes that don't require a claimed account."""
    tokens = guest(client)
    # We need a game mode — create one directly rather than depending on
    # a fixture from another file
    client.post(
        "/api/leaderboard/game_modes",
        json={"name": f"guest_test_{secrets.token_hex(3)}", "sort_order": "DESC", "label": "Test"},
        headers={"x-api-key": os.environ["API_KEY"]},
    )
    # Just verify the token is accepted — 404 on unknown mode is fine,
    # what we're ruling out is 401
    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 100, "game_mode": "classic"},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code != 401


# ── rename ─────────────────────────────────────────────────────────────────

def test_rename_happy_path(client):
    tokens = register(client)
    new_name = f"renamed_{secrets.token_hex(4)}"
    response = client.post(
        "/api/auth/rename",
        json={"username": new_name},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 204


def test_rename_taken_username_returns_409(client):
    tokens_a = register(client)
    user_b = random_user()
    register(client, user_b)
    response = client.post(
        "/api/auth/rename",
        json={"username": user_b["username"]},
        headers=bearer(tokens_a["access_token"]),
    )
    assert response.status_code == 409


def test_rename_requires_auth(client):
    response = client.post(
        "/api/auth/rename",
        json={"username": "anyone"},
    )
    assert response.status_code == 401


def test_guest_can_rename(client):
    tokens = guest(client)
    new_name = f"renamed_guest_{secrets.token_hex(4)}"
    response = client.post(
        "/api/auth/rename",
        json={"username": new_name},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 204


def test_rename_to_guest_username_returns_409(client):
    """Guest usernames should be protected from rename collision."""
    guest_tokens = guest(client)
    guest_username = decode_token(guest_tokens["access_token"])["username"]
    
    other_tokens = register(client)
    response = client.post(
        "/api/auth/rename",
        json={"username": guest_username},
        headers=bearer(other_tokens["access_token"]),
    )
    assert response.status_code == 409


# ── claim ──────────────────────────────────────────────────────────────────

def test_claim_upgrades_guest_account(client):
    tokens = guest(client)
    response = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 200


def test_claim_returns_non_guest_token(client):
    """Token returned after claim should reflect is_guest=False."""
    tokens = guest(client)
    response = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=bearer(tokens["access_token"]),
    )
    new_tokens = response.json()
    payload = decode_token(new_tokens["access_token"])
    assert payload["is_guest"] is False


def test_claim_already_claimed_returns_400(client):
    tokens = register(client)
    response = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 400


def test_claim_duplicate_email_returns_409(client):
    user = random_user()
    register(client, user)
    guest_tokens = guest(client)
    response = client.post(
        "/api/auth/claim",
        json={"email": user["email"], "password": "testpassword123"},
        headers=bearer(guest_tokens["access_token"]),
    )
    assert response.status_code == 409


def test_claim_requires_auth(client):
    response = client.post(
        "/api/auth/claim",
        json={"email": "anyone@example.com", "password": "testpassword123"},
    )
    assert response.status_code == 401


def test_claimed_account_can_submit_to_requires_auth_mode(client):
    """
    Verifies claim via behavior rather than token inspection —
    claimed account should be able to submit to a requires_claimed_account mode.
    """
    guest_tokens = guest(client)
    claimed_tokens = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=bearer(guest_tokens["access_token"]),
    ).json()

    mode_name = f"auth_mode_{secrets.token_hex(3)}"
    client.post(
        "/api/leaderboard/game_modes",
        json={"name": mode_name, "sort_order": "DESC", "label": "Auth Mode", "requires_claimed_account": True},
        headers={"x-api-key": os.environ["API_KEY"]},
    )

    response = client.post(
        "/api/leaderboard/scores",
        json={"score": 100, "game_mode": mode_name},
        headers=bearer(claimed_tokens["access_token"]),
    )
    assert response.status_code == 201