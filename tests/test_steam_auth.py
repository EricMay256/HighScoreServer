import pytest

from app import steam_auth
from app.steam_auth import (
    SteamAuthConfigError,
    SteamAuthInvalidTicket,
    SteamAuthUpstreamError,
    verify_steam_auth_ticket,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = steam_auth.httpx.Request("GET", steam_auth.STEAM_AUTHENTICATE_USER_TICKET_URL)
            response = steam_auth.httpx.Response(self.status_code, request=request)
            raise steam_auth.httpx.HTTPStatusError("error", request=request, response=response)

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    response: FakeResponse
    params: dict | None = None

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, params: dict) -> FakeResponse:
        assert url == steam_auth.STEAM_AUTHENTICATE_USER_TICKET_URL
        self.__class__.params = params
        return self.__class__.response


@pytest.fixture(autouse=True)
def steam_env(monkeypatch):
    monkeypatch.setenv("STEAM_WEB_API_KEY", "publisher-key")
    monkeypatch.setenv("STEAM_APP_ID", "12345")
    monkeypatch.setenv("STEAM_AUTH_IDENTITY", "hss-test")


async def test_verify_steam_auth_ticket_returns_steam_id(monkeypatch):
    FakeAsyncClient.response = FakeResponse(
        {"response": {"params": {"steamid": "76561198000000020"}}}
    )
    monkeypatch.setattr(steam_auth.httpx, "AsyncClient", FakeAsyncClient)

    steam_id = await verify_steam_auth_ticket("ticket-hex")

    assert steam_id == "76561198000000020"
    assert FakeAsyncClient.params == {
        "key": "publisher-key",
        "appid": 12345,
        "ticket": "ticket-hex",
        "identity": "hss-test",
    }


async def test_verify_steam_auth_ticket_rejects_error_response(monkeypatch):
    FakeAsyncClient.response = FakeResponse({"response": {"error": "Invalid ticket"}})
    monkeypatch.setattr(steam_auth.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(SteamAuthInvalidTicket):
        await verify_steam_auth_ticket("bad-ticket")


async def test_verify_steam_auth_ticket_requires_steam_id(monkeypatch):
    FakeAsyncClient.response = FakeResponse({"response": {"params": {}}})
    monkeypatch.setattr(steam_auth.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(SteamAuthInvalidTicket):
        await verify_steam_auth_ticket("ticket-without-id")


async def test_verify_steam_auth_ticket_maps_403_to_invalid_ticket(monkeypatch):
    FakeAsyncClient.response = FakeResponse({}, status_code=403)
    monkeypatch.setattr(steam_auth.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(SteamAuthInvalidTicket):
        await verify_steam_auth_ticket("bad-ticket")


async def test_verify_steam_auth_ticket_maps_500_to_upstream_error(monkeypatch):
    FakeAsyncClient.response = FakeResponse({}, status_code=500)
    monkeypatch.setattr(steam_auth.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(SteamAuthUpstreamError):
        await verify_steam_auth_ticket("ticket")


async def test_verify_steam_auth_ticket_requires_config(monkeypatch):
    monkeypatch.delenv("STEAM_WEB_API_KEY")

    with pytest.raises(SteamAuthConfigError):
        await verify_steam_auth_ticket("ticket")
