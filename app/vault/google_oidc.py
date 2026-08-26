"""Google OpenID Connect as an operator identity method for vault OAuth.

This authenticates the human approving a vault client; it does not issue the
client's vault credential. The outer authorization server still mints the
authorization code and the ordinary ``hssv1_`` access credential.

Google's authorization code is exchanged over async HTTP, then the returned ID
token is verified locally against Google's JWKS. Signature, issuer, audience,
expiry, nonce, verified email, and the configured allowlist all have to agree.
No token or email is logged.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_CALLBACK_PATH = "/vault/login/google/callback"
GOOGLE_LOGIN_PATH = "/vault/login/google"
GOOGLE_HTTP_TIMEOUT_SECONDS = 5.0


class GoogleOIDCError(Exception):
    """The external identity assertion could not be trusted."""


@dataclass(frozen=True, slots=True)
class GoogleOIDCSettings:
    client_id: str
    client_secret: str
    allowed_emails: frozenset[str]

    @classmethod
    def from_environment(cls) -> GoogleOIDCSettings | None:
        """Return configured Google login, None when wholly absent.

        Partial configuration is an operator error rather than a silent
        password-only fallback. Otherwise a typo in one variable removes the
        intended identity method while leaving the deployment looking healthy.
        """

        names = {
            "VAULT_GOOGLE_OIDC_CLIENT_ID": (
                os.environ.get("VAULT_GOOGLE_OIDC_CLIENT_ID") or ""
            ).strip(),
            "VAULT_GOOGLE_OIDC_CLIENT_SECRET": (
                os.environ.get("VAULT_GOOGLE_OIDC_CLIENT_SECRET") or ""
            ).strip(),
            "VAULT_GOOGLE_OIDC_ALLOWED_EMAILS": (
                os.environ.get("VAULT_GOOGLE_OIDC_ALLOWED_EMAILS") or ""
            ).strip(),
        }
        configured = {name for name, value in names.items() if value}
        if not configured:
            return None
        if len(configured) != len(names):
            missing = sorted(set(names) - configured)
            raise RuntimeError(
                "Google operator login is partially configured; missing "
                + ", ".join(missing)
            )

        emails = frozenset(
            value.strip().lower()
            for value in names["VAULT_GOOGLE_OIDC_ALLOWED_EMAILS"].split(",")
            if value.strip()
        )
        if not emails or any("@" not in email or " " in email for email in emails):
            raise RuntimeError(
                "VAULT_GOOGLE_OIDC_ALLOWED_EMAILS must be a comma-separated "
                "list of email addresses"
            )
        return cls(
            client_id=names["VAULT_GOOGLE_OIDC_CLIENT_ID"],
            client_secret=names["VAULT_GOOGLE_OIDC_CLIENT_SECRET"],
            allowed_emails=emails,
        )


def authorization_url(
    settings: GoogleOIDCSettings,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
) -> str:
    """Google's authorization-code URL for identity-only scopes."""

    return GOOGLE_AUTHORIZATION_ENDPOINT + "?" + urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
        }
    )


async def authenticate_google_code(
    code: str,
    *,
    redirect_uri: str,
    expected_nonce: str,
    settings: GoogleOIDCSettings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Exchange and verify a Google code, returning the durable subject.

    The return value contains Google's stable ``sub`` rather than the email,
    because Google documents that email can change while ``sub`` does not.
    """

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS)
    try:
        token_response = await http.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        if not isinstance(token_payload, dict):
            raise GoogleOIDCError
        id_token = token_payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise GoogleOIDCError

        jwks_response = await http.get(GOOGLE_JWKS_ENDPOINT)
        jwks_response.raise_for_status()
        jwks = jwks_response.json()
        if not isinstance(jwks, dict):
            raise GoogleOIDCError
        access_token = token_payload.get("access_token")
        claims: dict[str, Any] = jwt.decode(
            id_token,
            jwks,
            algorithms=["RS256"],
            audience=settings.client_id,
            issuer=GOOGLE_ISSUERS,
            access_token=(access_token if isinstance(access_token, str) else None),
            options={
                "require_aud": True,
                "require_exp": True,
                "require_iss": True,
                "require_sub": True,
            },
        )

        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(
            nonce.encode("utf-8"), expected_nonce.encode("utf-8")
        ):
            raise GoogleOIDCError
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != settings.client_id:
            raise GoogleOIDCError
        verified = claims.get("email_verified")
        if verified is not True and verified != "true":
            raise GoogleOIDCError
        email = claims.get("email")
        if not isinstance(email, str) or email.strip().lower() not in settings.allowed_emails:
            raise GoogleOIDCError
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise GoogleOIDCError
        return f"operator:google:{subject}"
    except (httpx.HTTPError, ValueError, JWTError, KeyError) as error:
        raise GoogleOIDCError from error
    finally:
        if owns_client:
            await http.aclose()
