"""Schema tests for migration 0004 (auth_identities)."""
import os
import secrets

import psycopg
import psycopg.errors
import pytest


def get_conn():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def insert_user(**overrides) -> int:
    suffix = secrets.token_hex(4)
    cols = {
        "username": f"identity_test_{suffix}",
        "is_guest": False,
    }
    cols.update(overrides)
    placeholders = ", ".join(["%s"] * len(cols))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO users ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING id",
                tuple(cols.values()),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return user_id


def insert_identity(user_id: int, provider: str, provider_user_id: str) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_identities (user_id, provider, provider_user_id)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (user_id, provider, provider_user_id),
            )
            identity_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return identity_id


def test_auth_identity_accepts_multiple_providers_for_one_user(client):
    user_id = insert_user()

    insert_identity(user_id, "steam", "76561198000000004")
    insert_identity(user_id, "epic", "epic-account-id")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM auth_identities WHERE user_id = %s",
                (user_id,),
            )
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_auth_identity_unique_per_provider_subject(client):
    first_user = insert_user()
    second_user = insert_user()
    insert_identity(first_user, "steam", "76561198000000005")

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_identity(second_user, "steam", "76561198000000005")


def test_auth_identity_same_subject_allowed_across_providers(client):
    first_user = insert_user()
    second_user = insert_user()

    insert_identity(first_user, "steam", "same-provider-user-id")
    insert_identity(second_user, "epic", "same-provider-user-id")


def test_auth_identity_provider_format_rejects_uppercase(client):
    user_id = insert_user()

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_identity(user_id, "Steam", "76561198000000006")


def test_auth_identity_cascades_when_user_deleted(client):
    user_id = insert_user()
    insert_identity(user_id, "steam", "76561198000000007")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            cur.execute(
                "SELECT 1 FROM auth_identities WHERE user_id = %s",
                (user_id,),
            )
            assert cur.fetchone() is None
            conn.commit()
    finally:
        conn.close()
