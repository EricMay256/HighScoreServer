"""The MCP surface as a reviewed artifact, not an emergent one.

Fifteen tools, each with an input schema derived from a Python signature and
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

**On output schemas.** Every tool returns a Pydantic model, so every declared
schema describes its response. That was not free: a `-> dict[str, Any]`
annotation also produces a schema, but a permissive `{"type": "object",
"additionalProperties": true}` one -- enough to make the SDK emit
`structuredContent` beside the text block (see `test_mcp_budget`) while
promising a client nothing about what is inside. All fourteen then-existing
tools were that shape
until 2026-08-26. `test_no_tool_declares_a_permissive_output_schema` keeps
them from sliding back, because the slide is silent: the schema stays valid
and simply stops saying anything.

**On annotations.** Every tool carries `ToolAnnotations`, and they are claims
a client may act on -- `readOnlyHint` to decide what to run without asking,
`destructiveHint` to decide what to confirm. They are a hint layer rather than
a security boundary (that is scope-filtered `list_tools` plus the per-tool
check, ADR 0021), but a hint that flatters a tool invites a client to skip a
confirmation the operator wanted. `build_vault_mcp_server`'s docstring records
the two judgement calls.
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


_PROSE_KEYS = frozenset({"description", "title", "examples", "example", "default"})


def _bindable_schema(
    schema: Any, root: Any, *, seen: frozenset[str] = frozenset()
) -> Any:
    """One JSON Schema with its prose removed and its `$ref`s followed.

    The sibling input map pins property names and leaves their schemas alone,
    on the grounds that a description inside a property is prose by another
    route. That reasoning is about *prose*, and it was applied to outputs as
    though it were about structure -- so an output field could be renamed,
    retyped, or made optional and the snapshot still matched, which is most of
    what a generated client binds to.

    `$ref`s are resolved rather than recorded, because `#/$defs/Foo` pins the
    name of a nested model and not its shape; a field added to `Foo` has to
    move the snapshot. `seen` breaks the cycle a self-referencing model would
    otherwise create, and records the cycle rather than hiding it.
    """

    if isinstance(schema, list):
        return [_bindable_schema(item, root, seen=seen) for item in schema]
    if not isinstance(schema, dict):
        return schema

    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            return {"$recursive": reference}
        target = root
        for step in reference.lstrip("#/").split("/"):
            if not isinstance(target, dict):
                return {"$unresolved": reference}
            target = target.get(step, {})
        return _bindable_schema(target, root, seen=seen | {reference})

    return {
        key: _bindable_schema(value, root, seen=seen)
        for key, value in sorted(schema.items())
        if key not in _PROSE_KEYS
    }


def _tool_surface(tool: Any) -> dict[str, Any]:
    """The bindable part of one tool, with prose left out.

    Inputs and outputs are pinned to different depths, on purpose. An input
    property's *name* and top-level type are recorded but its schema is not:
    a description or an example inside it is prose by another route, and
    pinning it would reintroduce the churn this snapshot avoids.

    An output is pinned in full through `output_shape`, because there is no
    equivalent escape hatch on that side -- a client generated from this schema
    binds to every field name, type, requiredness and enum in it, and until
    2026-08-28 the snapshot recorded only that the output was `"object"`. A
    field could be renamed or retyped without moving the golden.
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
        "output_shape": _bindable_schema(output, output),
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


def test_no_tool_declares_a_permissive_output_schema() -> None:
    """A schema that permits any object is a schema that says nothing.

    `-> dict[str, Any]` produces exactly that, and it is the natural thing to
    write when adding a tool -- which is why this is a test rather than a
    convention. It is not caught by anything else: the declaration is valid,
    the SDK is happy, and clients simply get no contract.
    """

    permissive = [
        entry["name"]
        for entry in current_surface()
        if entry["output_schema_is_permissive"]
    ]

    assert permissive == [], (
        "these tools return `dict[str, Any]` or similar; give them a Pydantic "
        f"return type so the declared schema describes the response: {permissive}"
    )


def test_every_tool_declares_annotations() -> None:
    """Absent annotations are not neutral -- they withhold a safety signal.

    A client deciding what to run without confirmation reads `readOnlyHint`,
    and a surface that can retire notes and adjudicate review cases should
    answer that question rather than leave it null.
    """

    unannotated = [
        entry["name"] for entry in current_surface() if entry["annotations"] is None
    ]

    assert unannotated == [], (
        f"every tool needs ToolAnnotations; missing on: {unannotated}"
    )


def test_a_tool_that_can_destroy_is_annotated_as_destructive() -> None:
    """The hints that take judgement, asserted by name.

    `destructiveHint` describes what a tool *may* do, not what one call does.
    `vault_decide_review_case` reads as safe by that standard until you notice
    its 'rejected' path deletes the candidate -- which is how it shipped
    annotated non-destructive while `vault_decide_amendment_proposal`, whose
    accept path overwrites, was annotated correctly. Asserting only that
    annotations exist cannot catch a wrong one.
    """

    destructive = {
        "vault_update_note": "replaces the body",
        "vault_retire_note": "deletes the note",
        "vault_decide_review_case": "'rejected' deletes the candidate",
        "vault_decide_amendment_proposal": "'accepted' overwrites the target",
    }
    surface = {entry["name"]: entry for entry in current_surface()}

    for name, why in destructive.items():
        annotations = surface[name]["annotations"]
        assert annotations["destructive_hint"] is True, f"{name}: {why}"
        assert annotations["read_only_hint"] is False, name


def test_a_read_only_tool_is_not_annotated_destructive() -> None:
    """The other half, so the assertion above cannot be satisfied by marking
    everything destructive -- which would make the hint useless in the other
    direction."""

    surface = {entry["name"]: entry for entry in current_surface()}

    for name in ("vault_search", "vault_get_note", "vault_list_review_cases"):
        annotations = surface[name]["annotations"]
        assert annotations["read_only_hint"] is True, name
        assert annotations["destructive_hint"] is False, name


if __name__ == "__main__":
    _write_golden()
    print(f"wrote {GOLDEN}")
