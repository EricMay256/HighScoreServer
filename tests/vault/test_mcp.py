"""The MCP adapter: authentication, the scope-filtered surface, and derivation.

These are transport tests. That the governed write path deduplicates, that
search fuses two arms, and that the read policy excludes what it should are
covered against the HTTP surface and are the same code here -- the point of
having two thin adapters over one service layer is that they cannot disagree
about any of it. What can only be tested here is the part that is genuinely
new: that a credential's scopes decide which tools *exist*, and that a
contribution's idempotency key is derived rather than supplied.
"""

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.vault.auth import VaultScope
from app.vault.mcp import derive_idempotency_key
from app.vault.settings import vault_enabled
from tests.vault.test_routes import _drop, _issue


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="The vault MCP adapter is only mounted when VAULT_ENABLED is true",
)

_ENDPOINT = "/api/v1/vault/mcp/"
_HEADERS = {
    "Content-Type": "application/json",
    # Streamable HTTP may answer as JSON or as an SSE stream, so a client has to
    # advertise both. Omitting either is a 406 from the transport.
    "Accept": "application/json, text/event-stream",
}


def _rpc(
    client: TestClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Post one JSON-RPC call and return the parsed response body."""

    headers = dict(_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params

    response = client.post(_ENDPOINT, json=body, headers=headers)
    if response.status_code != 200:
        return response

    # The transport answers as SSE by default; the JSON-RPC payload is the
    # single `data:` frame.
    match = re.search(r"^data: (.*)$", response.text, re.MULTILINE)
    return json.loads(match.group(1) if match else response.text)


def _tool_names(payload: Any) -> list[str]:
    return [tool["name"] for tool in payload["result"]["tools"]]


def test_mcp_endpoint_refuses_an_unauthenticated_call(client: TestClient) -> None:
    """The mount carries its own authentication; it inherits none."""

    response = client.post(
        _ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_HEADERS,
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_mcp_endpoint_refuses_a_malformed_token(client: TestClient) -> None:
    response = client.post(
        _ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**_HEADERS, "Authorization": "Bearer not-a-vault-token"},
    )

    assert response.status_code == 401


def test_bare_path_redirects_to_the_transport_endpoint(client: TestClient) -> None:
    """The URL an operator types must reach the endpoint, not a bare 405.

    307 rather than 302 so the method and body survive: a JSON-RPC call is a
    POST, and a redirect that downgraded it to GET would fail confusingly.
    """

    response = client.post(
        "/api/v1/vault/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_HEADERS,
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == _ENDPOINT


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (
            (VaultScope.READ,),
            ["vault_search", "vault_get_note"],
        ),
        (
            (VaultScope.READ, VaultScope.WRITE),
            ["vault_search", "vault_get_note", "vault_contribute"],
        ),
        (
            (VaultScope.READ, VaultScope.WRITE, VaultScope.UPDATE, VaultScope.DELETE),
            [
                "vault_search",
                "vault_get_note",
                "vault_contribute",
                "vault_update_note",
                "vault_retire_note",
            ],
        ),
    ],
)
def test_tool_list_is_filtered_by_the_credential_scopes(
    client: TestClient,
    scopes: tuple[str, ...],
    expected: list[str],
) -> None:
    """A destructive tool a credential cannot use must not be *advertised*.

    This is the injection boundary, not a convenience. The corpus is untrusted
    input -- notes are written by agents and read by agents -- so a note saying
    "also retire <id>" is read inside an already-authenticated session, and no
    scope check intercepts it because the session's scopes are genuine. What
    stops it is the tool being absent from the surface the injected text can
    name.
    """

    credential_id, token = _issue(scopes)
    try:
        payload = _rpc(client, token, "tools/list")
    finally:
        _drop(credential_id)

    assert sorted(_tool_names(payload)) == sorted(expected)


def test_a_withheld_tool_is_still_refused_when_called_directly(
    client: TestClient,
) -> None:
    """Filtering the list is not the only gate.

    A client is free to call a tool it was never shown. Listing decides what an
    agent can *see*; the per-tool check decides what it can *do*, and dropping
    either would leave the other carrying a boundary alone.
    """

    credential_id, token = _issue((VaultScope.READ,))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {"name": "vault_retire_note", "arguments": {"note_id": "whatever"}},
        )
    finally:
        _drop(credential_id)

    rendered = json.dumps(payload)
    assert "vault:delete" in rendered or "isError" in rendered


def test_search_over_mcp_returns_the_same_shape_as_http(client: TestClient) -> None:
    credential_id, token = _issue((VaultScope.READ,))
    try:
        payload = _rpc(
            client,
            token,
            "tools/call",
            {"name": "vault_search", "arguments": {"query": "vault", "limit": 3}},
        )
    finally:
        _drop(credential_id)

    result = json.loads(payload["result"]["content"][0]["text"])
    assert result["query"] == "vault"
    assert result["vector_status"] in {"used", "not_configured", "failed"}
    assert isinstance(result["hits"], list)


def test_contribute_tool_does_not_ask_the_model_for_an_idempotency_key(
    client: TestClient,
) -> None:
    """The key is derived, so it must not appear in the tool's input schema.

    Asking a model to invent one produces a fresh value per attempt, which turns
    a network timeout into a duplicate note -- the exact failure the key exists
    to prevent.
    """

    credential_id, token = _issue((VaultScope.READ, VaultScope.WRITE))
    try:
        payload = _rpc(client, token, "tools/list")
    finally:
        _drop(credential_id)

    contribute = next(
        tool
        for tool in payload["result"]["tools"]
        if tool["name"] == "vault_contribute"
    )
    schema = contribute["inputSchema"]
    assert "idempotency_key" not in schema["properties"]
    assert sorted(schema["required"]) == ["body", "title"]


def test_derived_key_is_stable_across_key_order() -> None:
    """A retry must produce the same key, and JSON key order is not content."""

    first = derive_idempotency_key({"title": "T", "body": "B", "tags": ["x"]})
    reordered = derive_idempotency_key({"tags": ["x"], "body": "B", "title": "T"})

    assert first == reordered


def test_derived_key_changes_with_content() -> None:
    assert derive_idempotency_key(
        {"title": "T", "body": "B"}
    ) != derive_idempotency_key({"title": "T", "body": "different"})


def test_derived_key_satisfies_the_contribution_contract() -> None:
    """8..128 characters against ^[A-Za-z0-9._:-]+$, or the write is rejected."""

    key = derive_idempotency_key({"title": "T", "body": "B"})

    assert 8 <= len(key) <= 128
    assert re.fullmatch(r"[A-Za-z0-9._:-]+", key)
