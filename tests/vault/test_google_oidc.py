"""Google OIDC operator authentication, isolated from the outer OAuth flow."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.vault.google_oidc import (
    GOOGLE_JWKS_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    GoogleOIDCError,
    GoogleOIDCSettings,
    authenticate_google_code,
    authorization_url,
)


CLIENT_ID = "vault-google-client.apps.googleusercontent.com"
CLIENT_SECRET = "google-client-secret"
EMAIL = "operator@example.com"
NONCE = "pending-vault-authorization"


def _b64uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _signed_token(**overrides: object) -> tuple[str, dict[str, object]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": _b64uint(numbers.n),
        "e": _b64uint(numbers.e),
    }
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-subject-123",
        "email": EMAIL,
        "email_verified": True,
        "nonce": NONCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(overrides)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        jwt.encode(
            claims,
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key"},
        ),
        jwk,
    )


def _settings() -> GoogleOIDCSettings:
    return GoogleOIDCSettings(CLIENT_ID, CLIENT_SECRET, frozenset({EMAIL}))


def _client(token: str, jwk: dict[str, object]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(200, json={"id_token": token})
        if str(request.url) == GOOGLE_JWKS_ENDPOINT:
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_google_config_is_independently_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "VAULT_GOOGLE_OIDC_CLIENT_ID",
        "VAULT_GOOGLE_OIDC_CLIENT_SECRET",
        "VAULT_GOOGLE_OIDC_ALLOWED_EMAILS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert GoogleOIDCSettings.from_environment() is None


def test_google_config_refuses_a_partial_identity_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_GOOGLE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("VAULT_GOOGLE_OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("VAULT_GOOGLE_OIDC_ALLOWED_EMAILS", raising=False)

    with pytest.raises(RuntimeError, match="partially configured"):
        GoogleOIDCSettings.from_environment()


def test_google_config_normalizes_the_email_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_GOOGLE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("VAULT_GOOGLE_OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv(
        "VAULT_GOOGLE_OIDC_ALLOWED_EMAILS", " Operator@Example.com, second@example.com "
    )

    settings = GoogleOIDCSettings.from_environment()

    assert settings is not None
    assert settings.allowed_emails == {EMAIL, "second@example.com"}


def test_authorization_url_requests_identity_only_and_binds_state() -> None:
    url = authorization_url(
        _settings(),
        redirect_uri="https://vault.example/vault/login/google/callback",
        state="pending.csrf",
        nonce=NONCE,
    )

    assert "scope=openid+email" in url
    assert "state=pending.csrf" in url
    assert f"nonce={NONCE}" in url


def test_a_verified_allowlisted_google_identity_returns_its_stable_subject() -> None:
    token, jwk = _signed_token()

    async def authenticate() -> str:
        async with _client(token, jwk) as client:
            return await authenticate_google_code(
                "google-code",
                redirect_uri="https://vault.example/vault/login/google/callback",
                expected_nonce=NONCE,
                settings=_settings(),
                client=client,
            )

    assert asyncio.run(authenticate()) == "operator:google:google-subject-123"


@pytest.mark.parametrize(
    "claim",
    [
        {"nonce": "another-request"},
        {"email": "attacker@example.com"},
        {"email_verified": False},
        {"aud": "another-client"},
        {"azp": "another-client"},
    ],
)
def test_google_identity_refuses_any_broken_trust_binding(claim: dict[str, object]) -> None:
    token, jwk = _signed_token(**claim)

    async def authenticate() -> None:
        async with _client(token, jwk) as client:
            with pytest.raises(GoogleOIDCError):
                await authenticate_google_code(
                    "google-code",
                    redirect_uri="https://vault.example/vault/login/google/callback",
                    expected_nonce=NONCE,
                    settings=_settings(),
                    client=client,
                )

    asyncio.run(authenticate())
