import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from jose import jwt

from app import auth_routes
from app.auth import create_access_token
from app.auth_identities import NATIVE_AUTH_PROVIDER
from app.limiter import limiter
from app.steam_auth import (
    SteamAuthConfigError,
    SteamAuthInvalidTicket,
    SteamAuthUpstreamError,
)


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


def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def identity_rows_for_username(username: str) -> list[tuple[str, str]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ai.provider, ai.provider_user_id
                FROM auth_identities ai
                JOIN users u ON u.id = ai.user_id
                WHERE u.username = %s
                ORDER BY ai.provider, ai.provider_user_id
                """,
                (username,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def identity_rows_for_user_id(user_id: int) -> list[tuple[str, str]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, provider_user_id
                FROM auth_identities
                WHERE user_id = %s
                ORDER BY provider, provider_user_id
                """,
                (user_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


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


def test_register_creates_native_auth_identity(client):
    user = random_user()
    register(client, user)
    assert identity_rows_for_username(user["username"]) == [
        (NATIVE_AUTH_PROVIDER, user["email"])
    ]


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


# -- steam auth --------------------------------------------------------------

def test_steam_login_creates_steam_identity_user(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        assert ticket == "valid-ticket"
        return "76561198000000010"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    response = client.post("/api/auth/steam/login", json={"ticket": "valid-ticket"})

    assert response.status_code == 200
    payload = decode_token(response.json()["access_token"])
    assert payload["username"].startswith("steam_")
    assert payload["is_guest"] is False
    assert identity_rows_for_user_id(int(payload["sub"])) == [
        ("steam", "76561198000000010")
    ]


def test_steam_login_reuses_existing_steam_identity(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        return "76561198000000011"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    first = client.post("/api/auth/steam/login", json={"ticket": "ticket-one"})
    second = client.post("/api/auth/steam/login", json={"ticket": "ticket-two"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert decode_token(first.json()["access_token"])["sub"] == decode_token(
        second.json()["access_token"]
    )["sub"]


def test_steam_link_adds_second_provider_to_native_user(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        return "76561198000000012"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)
    user = random_user()
    tokens = register(client, user)
    user_id = int(decode_token(tokens["access_token"])["sub"])

    response = client.post(
        "/api/auth/steam/link",
        json={"ticket": "valid-ticket"},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200
    assert decode_token(response.json()["access_token"])["sub"] == str(user_id)
    assert identity_rows_for_user_id(user_id) == [
        ("steam", "76561198000000012"),
        (NATIVE_AUTH_PROVIDER, user["email"]),
    ]


def test_steam_link_upgrades_guest_account(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        return "76561198000000013"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)
    tokens = guest(client)
    user_id = int(decode_token(tokens["access_token"])["sub"])

    response = client.post(
        "/api/auth/steam/link",
        json={"ticket": "valid-ticket"},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200
    payload = decode_token(response.json()["access_token"])
    assert payload["sub"] == str(user_id)
    assert payload["is_guest"] is False
    assert identity_rows_for_user_id(user_id) == [("steam", "76561198000000013")]


def test_steam_link_rejects_identity_owned_by_another_user(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        return "76561198000000014"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)
    first = register(client)
    second = register(client)
    client.post(
        "/api/auth/steam/link",
        json={"ticket": "first-link"},
        headers=bearer(first["access_token"]),
    )

    response = client.post(
        "/api/auth/steam/link",
        json={"ticket": "second-link"},
        headers=bearer(second["access_token"]),
    )

    assert response.status_code == 409


def test_steam_auth_invalid_ticket_returns_401(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        raise SteamAuthInvalidTicket("bad ticket")

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    response = client.post("/api/auth/steam/login", json={"ticket": "bad-ticket"})

    assert response.status_code == 401


def test_steam_auth_missing_config_returns_503(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        raise SteamAuthConfigError("missing config")

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    response = client.post("/api/auth/steam/login", json={"ticket": "valid-ticket"})

    assert response.status_code == 503


def test_steam_auth_upstream_error_returns_502(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        raise SteamAuthUpstreamError("upstream down")

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    response = client.post("/api/auth/steam/login", json={"ticket": "valid-ticket"})

    assert response.status_code == 502


def test_steam_link_requires_auth(client, monkeypatch):
    async def fake_verify(ticket: str) -> str:
        return "76561198000000015"

    monkeypatch.setattr("app.auth_routes.verify_steam_auth_ticket", fake_verify)

    response = client.post("/api/auth/steam/link", json={"ticket": "valid-ticket"})

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
    assert response.status_code == 200


def test_rename_returns_a_token_carrying_the_new_username(client: TestClient) -> None:
    """The access token carries `username`, so a rename must reissue it.

    /rename used to return 204, leaving every client showing the old name
    until the access token expired and a refresh happened to mint a new one.
    """

    tokens = register(client)
    new_name = f"renamed_{secrets.token_hex(4)}"

    response = client.post(
        "/api/auth/rename",
        json={"username": new_name},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert decode_token(body["access_token"])["username"] == new_name
    # The access token only. A refresh token carries no username, so a rename
    # does not invalidate it and there is nothing here to replace.
    assert "refresh_token" not in body

    # The caller's existing refresh token still works and still belongs to the
    # same account, so the pair it now holds does not disagree with itself.
    refreshed = client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert decode_token(refreshed.json()["access_token"])["username"] == new_name


def test_renaming_repeatedly_mints_no_extra_refresh_tokens(
    client: TestClient,
) -> None:
    """Rename used to add a live credential every time it was called.

    It returned a full TokenResponse, minting a refresh token through
    `create_refresh_token`, which inserts without revoking anything. The
    previous token stayed valid for its full lifetime, so five renames left
    six usable credentials and five surplus rows -- unbounded, because nothing
    limits how often a client may rename.

    /claim has the same shape and is safe from it only because it can succeed
    once per account.
    """

    tokens = register(client)
    user_id = int(decode_token(tokens["access_token"])["sub"])

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM refresh_tokens WHERE user_id = %s", (user_id,)
            )
            before = cur.fetchone()[0]
    finally:
        conn.close()

    access = tokens["access_token"]
    for index in range(3):
        response = client.post(
            "/api/auth/rename",
            json={"username": f"norotate_{secrets.token_hex(4)}_{index}"},
            headers=bearer(access),
        )
        assert response.status_code == 200
        access = response.json()["access_token"]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM refresh_tokens WHERE user_id = %s", (user_id,)
            )
            after = cur.fetchone()[0]
    finally:
        conn.close()

    assert after == before, "a rename minted a refresh token nobody asked for"


def test_rename_preserves_guest_status_in_the_reissued_token(client: TestClient) -> None:
    """Renaming is not a claim: `is_guest` must survive the reissue.

    The new token is built from the row the UPDATE returns rather than from
    the old token's claims, so this pins that the row's `is_guest` is what
    reaches it -- a rename that silently promoted a guest would hand out
    access to guest-gated modes.
    """

    tokens = guest(client)
    new_name = f"renamed_guest_{secrets.token_hex(4)}"

    response = client.post(
        "/api/auth/rename",
        json={"username": new_name},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200
    claims = decode_token(response.json()["access_token"])
    assert claims["username"] == new_name
    assert claims["is_guest"] is True


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
    assert response.status_code == 200


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


def test_claim_creates_native_auth_identity_on_guest_user(client):
    tokens = guest(client)
    username = decode_token(tokens["access_token"])["username"]
    email = f"claim_{secrets.token_hex(4)}@example.com"
    response = client.post(
        "/api/auth/claim",
        json={"email": email, "password": "testpassword123"},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 200
    assert identity_rows_for_username(username) == [(NATIVE_AUTH_PROVIDER, email)]


def test_claim_already_claimed_returns_400(client):
    tokens = register(client)
    response = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=bearer(tokens["access_token"]),
    )
    assert response.status_code == 400


def test_claim_rejects_replay_of_original_guest_token(client):
    tokens = guest(client)
    headers = bearer(tokens["access_token"])

    first = client.post(
        "/api/auth/claim",
        json={"email": f"claim_{secrets.token_hex(4)}@example.com", "password": "testpassword123"},
        headers=headers,
    )
    replay = client.post(
        "/api/auth/claim",
        json={"email": f"replay_{secrets.token_hex(4)}@example.com", "password": "different-password"},
        headers=headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert replay.json()["detail"] == "Account is already claimed"


def test_concurrent_guest_claims_allow_exactly_one_winner(client):
    tokens = guest(client)
    username = decode_token(tokens["access_token"])["username"]
    headers = bearer(tokens["access_token"])
    emails = [f"claim_{secrets.token_hex(4)}@example.com" for _ in range(2)]

    def submit_claim(email: str) -> Response:
        return client.post(
            "/api/auth/claim",
            json={"email": email, "password": "testpassword123"},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit_claim, emails))

    assert sorted(response.status_code for response in responses) == [200, 400]
    identities = identity_rows_for_username(username)
    assert len(identities) == 1
    assert identities[0][0] == NATIVE_AUTH_PROVIDER
    assert identities[0][1] in emails


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


def test_replaying_a_guest_token_after_claiming_does_not_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim path must not pay bcrypt to discover there is nothing to do.

    A successful claim does not revoke the guest token that authorized it, and
    the JWT's `is_guest` claim stays true for the rest of its hour. The handler
    trusted that claim, hashed the password, and only then learned from the
    UPDATE that the account was already claimed -- so replaying one token drove
    an unbounded number of bcrypt rounds, each occupying an executor thread,
    on a route with no rate limit.

    Hashing is the amplifier, so hashing is what this asserts on: the replay
    must be refused without calling it.
    """

    calls: list[str] = []
    original = auth_routes.hash_password

    async def counting_hash(plain: str) -> str:
        calls.append(plain)
        return await original(plain)

    monkeypatch.setattr(auth_routes, "hash_password", counting_hash)

    tokens = guest(client)
    body = {
        "email": f"replay_{secrets.token_hex(4)}@example.com",
        "password": "testpassword123",
    }

    first = client.post("/api/auth/claim", json=body, headers=bearer(tokens["access_token"]))
    assert first.status_code == 200
    assert len(calls) == 1, "the real claim should hash exactly once"

    # Same token, still carrying is_guest=true, now naming a claimed account.
    for _ in range(3):
        replay = client.post(
            "/api/auth/claim",
            json={**body, "email": f"replay_{secrets.token_hex(4)}@example.com"},
            headers=bearer(tokens["access_token"]),
        )
        assert replay.status_code == 400
        assert replay.json()["detail"] == "Account is already claimed"

    assert len(calls) == 1, "a replayed guest token reached bcrypt"


# The /claim bucket, and a burst comfortably past twice it -- see the test.
CLAIM_LIMIT_PER_MINUTE = 5
ATTEMPTS = 12


def test_claim_is_actually_refused_once_its_bucket_empties(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforcement, not documentation.

    This replaces an assertion on the OpenAPI 429, which was declared by hand
    in `responses=` and therefore survived removing `@limiter.limit` entirely
    -- verified: the route kept its documented 429 while enforcing nothing.
    A test that passes with the protection deleted is worse than no test.

    The suite disables the limiter globally, so this enables it for one test
    and resets the buckets either side. The guest is created first, while the
    limiter is still off, because /guest has a 5/minute bucket of its own and
    would otherwise spend part of what is being measured.

    The burst is deliberately more than twice the limit. slowapi's default
    storage is a fixed window, so a burst that straddles a minute boundary
    gets a fresh allowance partway through -- which made an exact
    "the sixth request is refused" assertion flake once in a full-suite run.
    With more than 2x the limit in under a second, one boundary can fall
    anywhere and still leave one side of it over the limit, so a refusal is
    guaranteed without the test knowing where the boundary is.
    """

    tokens = guest(client)
    body = {
        "email": f"bucket_{secrets.token_hex(4)}@example.com",
        "password": "testpassword123",
    }

    limiter.reset()
    monkeypatch.setattr(limiter, "enabled", True)
    try:
        statuses = []
        for _ in range(ATTEMPTS):
            reply = client.post(
                "/api/auth/claim",
                json={**body, "email": f"bucket_{secrets.token_hex(4)}@example.com"},
                headers=bearer(tokens["access_token"]),
            )
            statuses.append(reply.status_code)
    finally:
        limiter.reset()

    assert statuses[0] == 200, f"the first claim should succeed: {statuses}"
    assert 429 in statuses, f"the bucket never refused anything: {statuses}"

    # Refused by the bucket only after it had let the limit through, so this
    # also catches a limit set far tighter than intended.
    first_refusal = statuses.index(429)
    assert first_refusal >= CLAIM_LIMIT_PER_MINUTE, (
        f"refused after only {first_refusal} requests: {statuses}"
    )
    # Everything before it was refused on account state, not by the bucket --
    # the two guards are distinct and this says which did the work.
    assert set(statuses[1:first_refusal]) <= {400}, (
        f"expected state refusals before the bucket engaged: {statuses}"
    )


def test_rename_invalidates_the_leaderboard_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaderboard rows carry the username, and cached ones carry the old one.

    Without this the header updates from the new token immediately while the
    board underneath keeps the previous name for the 120s TTL -- one page
    disagreeing with itself about who the reader is.

    The whole `leaderboard:` namespace rather than this user's modes: a rename
    does not say which boards they appear on, and rename is rare enough that
    refilling the cache costs less than the query to find out.
    """

    deleted: list[str] = []

    class RecordingCache:
        async def delete_prefix(self, prefix: str) -> int:
            deleted.append(prefix)
            return 0

    monkeypatch.setattr(auth_routes, "get_cache", lambda: RecordingCache())

    tokens = register(client)
    response = client.post(
        "/api/auth/rename",
        json={"username": f"cachebust_{secrets.token_hex(4)}"},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200
    assert deleted == ["leaderboard:"]


def test_rename_survives_an_unavailable_cache(client: TestClient) -> None:
    """Invalidation is best effort; a dead cache is a stale name, not a 500.

    The suite runs with `get_cache` raising, so every other rename test already
    exercises this path -- but that is incidental, and this states it, because
    the failure it guards against is a rename that reports failure after having
    already renamed the user.
    """

    tokens = register(client)
    response = client.post(
        "/api/auth/rename",
        json={"username": f"nocache_{secrets.token_hex(4)}"},
        headers=bearer(tokens["access_token"]),
    )

    assert response.status_code == 200


def test_rename_never_extends_the_access_token(client: TestClient) -> None:
    """A repeatable reissue must not renew the session it was authorized by.

    /rename is authorized by an access token and may be called as often as its
    bucket allows. `create_access_token` sets `exp` an hour out, so returning a
    default-lifetime token let any still-valid one mint a successor with a
    fresh hour -- indefinitely, and a stolen token equally, long after the
    refresh token it descended from had expired or been revoked. The refresh
    token would stop being what bounds a session.

    The reissue exists to correct the `username` claim. It carries the deadline
    it was given.
    """

    tokens = register(client)
    claims = decode_token(tokens["access_token"])

    # Deliberately not the token `register` handed back. Its `exp` is an hour
    # out, and so is a freshly minted one, so within the same second the two
    # are equal and the assertion holds whether or not the deadline is carried
    # forward -- which is exactly how the first version of this test passed
    # against the bug. A five-minute deadline cannot be confused with a fresh
    # hour.
    access = create_access_token(
        int(claims["sub"]),
        claims["username"],
        is_guest=claims["is_guest"],
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    original_exp = decode_token(access)["exp"]
    fresh_hour = int((datetime.now(UTC) + timedelta(minutes=60)).timestamp())
    assert original_exp < fresh_hour - 60, "the fixture must be distinguishable"

    for index in range(3):
        reply = client.post(
            "/api/auth/rename",
            json={"username": f"noextend_{secrets.token_hex(4)}_{index}"},
            headers=bearer(access),
        )
        assert reply.status_code == 200
        access = reply.json()["access_token"]

        reissued = decode_token(access)
        assert reissued["exp"] == original_exp, (
            f"rename {index} moved the expiry: {reissued['exp']} != {original_exp}"
        )
