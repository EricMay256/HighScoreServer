import os
from dataclasses import dataclass

import httpx


STEAM_AUTH_PROVIDER = "steam"
STEAM_AUTHENTICATE_USER_TICKET_URL = (
    "https://partner.steam-api.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
)


class SteamAuthError(ValueError):
    """Base error for Steam authentication failures."""


class SteamAuthConfigError(SteamAuthError):
    """Raised when Steam auth is not configured on this server."""


class SteamAuthInvalidTicket(SteamAuthError):
    """Raised when Steam rejects or cannot resolve a submitted ticket."""


class SteamAuthUpstreamError(SteamAuthError):
    """Raised when Steam auth cannot be reached or returns an invalid response."""


@dataclass(frozen=True)
class SteamAuthConfig:
    web_api_key: str
    app_id: int
    identity: str


def get_steam_auth_config() -> SteamAuthConfig:
    key = os.environ.get("STEAM_WEB_API_KEY")
    app_id_raw = os.environ.get("STEAM_APP_ID")
    identity = os.environ.get("STEAM_AUTH_IDENTITY", "hss")

    if not key or not app_id_raw:
        raise SteamAuthConfigError("Steam authentication is not configured")

    try:
        app_id = int(app_id_raw)
    except ValueError as exc:
        raise SteamAuthConfigError("STEAM_APP_ID must be an integer") from exc

    if app_id <= 0:
        raise SteamAuthConfigError("STEAM_APP_ID must be positive")

    return SteamAuthConfig(web_api_key=key, app_id=app_id, identity=identity)


async def verify_steam_auth_ticket(ticket: str) -> str:
    """
    Validate a Steam Web API auth ticket and return the user's SteamID64.

    The client obtains `ticket` from Steamworks `GetAuthTicketForWebApi` using
    the configured identity string. This server then validates the ticket with
    Steam's publisher-key-only Web API and treats only Steam's returned SteamID64
    as the account subject.
    """
    config = get_steam_auth_config()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                STEAM_AUTHENTICATE_USER_TICKET_URL,
                params={
                    "key": config.web_api_key,
                    "appid": config.app_id,
                    "ticket": ticket,
                    "identity": config.identity,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400, 401, 403}:
            raise SteamAuthInvalidTicket("Steam rejected the auth ticket") from exc
        raise SteamAuthUpstreamError("Steam auth returned an error") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SteamAuthUpstreamError("Steam auth request failed") from exc

    error = payload.get("response", {}).get("error")
    if error:
        raise SteamAuthInvalidTicket(str(error))

    steam_id = payload.get("response", {}).get("params", {}).get("steamid")
    if not isinstance(steam_id, str) or not steam_id.isdecimal():
        raise SteamAuthInvalidTicket("Steam auth response did not include a SteamID64")

    return steam_id
