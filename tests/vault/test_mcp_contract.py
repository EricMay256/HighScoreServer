"""The MCP surface as a reviewed artifact, not an emergent one.

Fourteen tools, each with an input schema derived from a Python signature and
an output schema derived from a return annotation. Both are generated, which
makes them easy to change by accident: renaming a parameter, giving a default,
or widening a return type all edit the published contract without touching
anything that looks like a contract.

This module pins that surface to a checked-in snapshot so a change to it
arrives as a reviewable diff. It is deliberately *not* a snapshot of the whole
`tools/list` payload -- descriptions are prose, they are edited often and on
purpose, and including them would make the golden churn on every wording fix
until nobody read the diff. What is pinned is the part a client binds to:
which tools exist, what they require, what they accept, whether they declare
structured output, and how they are annotated.

**On output schemas.** Every tool here currently reports `object`, and none of
that is deliberate design -- it falls out of `-> dict[str, Any]`, from which
`func_metadata` derives a permissive `{"type": "object",
"additionalProperties": true}`. That is a real declared schema with no
information in it: it makes the SDK emit `structuredContent` beside the text
block (see `test_mcp_budget`) while promising a client nothing about what is
inside. Replacing those annotations with typed models is what turns the
declaration into a contract, and this snapshot is how that change is reviewed.

**On annotations.** All fourteen report `None`. `ToolAnnotations` is where a
server says whether a tool is read-only, destructive or idempotent, and a
client may use those hints to decide what to call without confirmation. A
surface that retires notes and adjudicates review cases should not leave that
field empty, and this snapshot records that it currently does.
"""

import json
import pathlib
from typing import Any

import pytest

from app.vault.mcp import _TOOL_SCOPES, build_vault_mcp_server
from app.vault.settings import vault_enabled


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="The vault MCP adapter is only mounted when VAULT_ENABLED is true",
)

GOLDEN = pathlib.Path(__file__).with_name("mcp_surface.json")

REGENERATE = (
    "The published MCP surface changed. If that was the intent, regenerate the "
    f"snapshot with:\n\n    python -m tests.vault.test_mcp_contract\n\n"
    f"and review the diff to {GOLDEN.name} as part of the change."
)


def _tool_surface(tool: Any) -> dict[str, Any]:
    """The bindable part of one tool, with prose left out.

    Property *names* are pinned but their schemas are not: a description or an
    example inside a property is prose by another route, and pinning it would
    reintroduce exactly the churn this snapshot avoids. A type change to a
    property is caught by the sibling type map rather than by the whole blob.
    """

    parameters = tool.parameters or {}
    properties = parameters.get("properties", {}) or {}
    output = tool.output_schema or None
    annotations = tool.annotations

    return {
        "name": tool.name,
        "scope": _TOOL_SCOPES[tool.name][0],
        "quota": _TOOL_SCOPES[tool.name][1],
        "required": sorted(parameters.get("required", []) or []),
        "properties": sorted(properties),
        "property_types": {
            name: schema.get("type", schema.get("anyOf", "unspecified"))
            for name, schema in sorted(properties.items())
            if isinstance(schema, dict)
        },
        "output_schema_type": None if output is None else output.get("type"),
        "output_schema_is_permissive": (
            output is not None
            and output.get("additionalProperties") is True
            and "properties" not in output
        ),
        "annotations": (
            None
            if annotations is None
            else annotations.model_dump(exclude_none=True)
        ),
    }


def current_surface() -> list[dict[str, Any]]:
    """Every tool the server registers, before any scope filtering.

    `VaultMCPServer.list_tools` narrows to the ambient credential's scopes,
    which is a security boundary tested elsewhere. The contract snapshot wants
    the whole surface, so it reads the registry directly -- otherwise a tool
    could be removed from the published surface and the golden would record
    only that this test's credential could not see it.
    """

    server = build_vault_mcp_server()
    tools = server._tool_manager.list_tools()
    return sorted(
        (_tool_surface(tool) for tool in tools),
        key=lambda entry: entry["name"],
    )


def _write_golden() -> None:
    GOLDEN.write_text(
        json.dumps(current_surface(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_the_published_surface_matches_its_snapshot() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert current_surface() == expected, REGENERATE


def test_every_registered_tool_has_a_declared_scope() -> None:
    """A tool absent from `_TOOL_SCOPES` is invisible, not open.

    `list_tools` filters on membership in that map, so an unlisted tool is
    never advertised -- but `_authorized` reads the same map, so it also
    cannot be called. The failure is a tool that silently does not exist,
    which is worth catching at the point of registration rather than in a
    session where someone wonders where it went.
    """

    server = build_vault_mcp_server()
    registered = {tool.name for tool in server._tool_manager.list_tools()}

    assert registered == set(_TOOL_SCOPES), (
        "every registered tool needs an entry in _TOOL_SCOPES, and every "
        "entry needs a registered tool"
    )


if __name__ == "__main__":
    _write_golden()
    print(f"wrote {GOLDEN}")
