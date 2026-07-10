import os
import secrets

import psycopg
import pytest
from jose import jwt

from app.auth_identities import (
    AuthIdentityConflict,
    attach_auth_identity_to_user,
    find_user_by_auth_identity,
    resolve_auth_identity_login,
)


def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def decode_token(token: str) -> dict:
    return jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])


def identity_count(user_id: int) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM auth_identities WHERE user_id = %s",
                (user_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def is_guest(user_id: int) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_guest FROM users WHERE id = %s", (user_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def register(client) -> int:
    suffix = secrets.token_hex(4)
    response = client.post(
        "/api/auth/register",
        json={
            "username": f"user_{suffix}",
            "email": f"user_{suffix}@example.com",
            "password": "testpassword123",
        },
    )
    assert response.status_code == 201
    return int(decode_token(response.json()["access_token"])["sub"])


def guest(client) -> int:
    response = client.post("/api/auth/guest")
    assert response.status_code == 201
    return int(decode_token(response.json()["access_token"])["sub"])


def test_resolve_external_identity_creates_non_guest_user(client):
    user = client.portal.call(
        resolve_auth_identity_login,
        "steam",
        "76561198000000001",
    )

    assert user.username.startswith("steam_")
    assert user.is_guest is False
    assert identity_count(user.id) == 1
    assert is_guest(user.id) is False


def test_resolve_external_identity_returns_existing_user(client):
    provider_user_id = "76561198000000002"
    first = client.portal.call(resolve_auth_identity_login, "steam", provider_user_id)
    second = client.portal.call(resolve_auth_identity_login, "steam", provider_user_id)

    assert second == first


def test_attach_identity_adds_second_provider_to_existing_user(client):
    user_id = register(client)

    linked = client.portal.call(
        attach_auth_identity_to_user,
        user_id,
        "epic",
        f"epic-{secrets.token_hex(8)}",
    )

    assert linked.id == user_id
    assert linked.is_guest is False
    assert identity_count(user_id) == 2


def test_attach_identity_upgrades_guest_in_place(client):
    user_id = guest(client)

    linked = client.portal.call(
        attach_auth_identity_to_user,
        user_id,
        "steam",
        "76561198000000003",
    )

    assert linked.id == user_id
    assert linked.is_guest is False
    assert is_guest(user_id) is False


def test_attach_identity_rejects_identity_owned_by_another_user(client):
    first_user = register(client)
    second_user = register(client)
    provider_user_id = f"epic-{secrets.token_hex(8)}"
    client.portal.call(attach_auth_identity_to_user, first_user, "epic", provider_user_id)

    with pytest.raises(AuthIdentityConflict):
        client.portal.call(
            attach_auth_identity_to_user,
            second_user,
            "epic",
            provider_user_id,
        )


def test_different_providers_can_reuse_provider_user_id(client):
    provider_user_id = "shared-subject-id"
    steam_user = client.portal.call(
        resolve_auth_identity_login,
        "steam",
        provider_user_id,
    )
    epic_user = client.portal.call(
        resolve_auth_identity_login,
        "epic",
        provider_user_id,
    )

    assert steam_user.id != epic_user.id
    assert (
        client.portal.call(find_user_by_auth_identity, "steam", provider_user_id)
        == steam_user
    )
    assert (
        client.portal.call(find_user_by_auth_identity, "epic", provider_user_id)
        == epic_user
    )
