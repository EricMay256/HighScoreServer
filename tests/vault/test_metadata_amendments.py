"""Amendments that change edges and classification without resending the note.

The kind exists because every other edit path is a full replacement: adding one
edge required sending the title and body back, so a metadata change was
indistinguishable from a content rewrite — to the schema, to a reviewer reading
the proposal, and to anyone who mistyped a character on the way. See ADR 0036.

The assertions that matter are the negative ones. This kind's entire claim is
that it *cannot* touch what is embedded, so the tests that earn their place are
the ones proving the title, body, tags, and aliases are unreachable through it.
"""

import pytest

from app.vault.api_models import VaultMetadataChange, amendment_proposal_change
from app.vault.domain import AmendmentProposalKind
from app.vault.service import MetadataChange, VaultAmendmentService


class _Target:
    """A stored note as `_metadata_update` reads it."""

    id = "note-1"
    title = "The title that must survive"
    body = "The body that must survive.\n"
    summary = "A precis."
    tags = ("alpha", "beta")
    aliases = ("Another name",)
    facets = {"area": ["architecture"]}
    related_ids = ("old-1",)
    source_ids = ("src-1",)
    source_url = "https://example.invalid/original"


def _update(change: MetadataChange):
    return VaultAmendmentService._metadata_update(
        change, _Target(), principal_id="agent:test", request_id="req-1"
    )


def test_an_omitted_field_is_left_alone() -> None:
    """Omitted means unchanged, which is what lets a caller add one edge
    without restating the fields it is not touching."""

    result = _update(MetadataChange(related_ids=("new-1", "new-2")))

    assert result.related_ids == ("new-1", "new-2")
    assert result.source_ids == _Target.source_ids
    assert result.facets == _Target.facets
    assert result.source_url == _Target.source_url


def test_an_empty_list_clears_the_field() -> None:
    """The other half of the rule: empty is a value, not an omission."""

    result = _update(MetadataChange(related_ids=()))

    assert result.related_ids == ()
    assert result.source_ids == _Target.source_ids


def test_the_title_body_tags_and_aliases_are_unreachable() -> None:
    """The claim the kind is built on.

    Tags and aliases join `assemble_embedding_text`, so if either could be
    changed here the operation could alter what the note means to search while
    presenting itself as a metadata edit — and would need a re-embed and a
    dedup run it does not perform.
    """

    result = _update(
        MetadataChange(
            related_ids=("new-1",),
            source_ids=("new-src",),
            facets={"area": ["gameplay"]},
        )
    )

    assert result.title == _Target.title
    assert result.body == _Target.body
    assert result.tags == _Target.tags
    assert result.aliases == _Target.aliases
    assert result.summary == _Target.summary


def test_clearing_the_source_url_is_distinct_from_omitting_it() -> None:
    """`None` cannot mean both "leave it" and "remove it", so the flag says
    which was meant."""

    assert _update(MetadataChange(related_ids=())).source_url == _Target.source_url
    assert _update(MetadataChange(clear_source_url=True)).source_url is None


def test_an_empty_change_is_refused() -> None:
    assert MetadataChange().is_empty()
    with pytest.raises(ValueError, match="at least one field"):
        VaultAmendmentService._metadata_payload(MetadataChange())


def test_the_payload_carries_only_what_was_set() -> None:
    """A reviewer should see the change, not a document with differences in it."""

    payload = VaultAmendmentService._metadata_payload(
        MetadataChange(related_ids=("a", "b"))
    )

    assert payload == {"related_ids": ["a", "b"]}


def test_a_cleared_source_url_survives_the_payload_round_trip() -> None:
    """`source_url: null` in the payload has to stay distinguishable from an
    absent key, or clearing degrades to omitting on the way to storage."""

    payload = VaultAmendmentService._metadata_payload(
        MetadataChange(clear_source_url=True)
    )

    assert payload == {"source_url": None}
    assert "source_url" in payload


class _Proposal:
    change_kind = AmendmentProposalKind.METADATA
    change = {"related_ids": ["a", "b"], "facets": {"area": ["architecture"]}}


def test_a_stored_metadata_proposal_renders_as_a_metadata_change() -> None:
    """The review surface has to show it as what it is rather than falling
    through to the replacement branch, which would report a document."""

    rendered = amendment_proposal_change(_Proposal())

    assert isinstance(rendered, VaultMetadataChange)
    assert rendered.kind == "metadata"
    assert rendered.related_ids == ["a", "b"]
    assert rendered.source_ids is None


def test_the_schema_refuses_a_metadata_payload_touching_embedded_fields() -> None:
    """The constraint is the claim, so the claim is tested where it lives.

    Python could be bypassed by any future caller building a proposal row
    directly; the CHECK cannot. It enumerates the four permitted keys precisely
    so that `tags` in a metadata payload is a write that fails rather than a
    retrieval change nobody sees.
    """

    import os
    from uuid import uuid4

    import psycopg

    url = os.environ["TEST_DATABASE_URL"]
    accepted, refused = [], []
    cases = {
        "related_ids only": '{"related_ids": ["a"]}',
        "facets only": '{"facets": {"area": ["x"]}}',
        "cleared source_url": '{"source_url": null}',
        "tags (embedded)": '{"tags": ["x"]}',
        "aliases (embedded)": '{"aliases": ["x"]}',
        "body smuggled in": '{"related_ids": ["a"], "body": "rewritten"}',
        "empty object": "{}",
    }
    connection = psycopg.connect(url)
    try:
        for label, payload in cases.items():
            proposal_id = uuid4()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO vault.vault_amendment_proposals
                            (id, target_document_id, target_revision, change_kind,
                             change, rationale, proposed_by, state)
                        VALUES (%s, 'note-1', 1, 'metadata', %s::jsonb,
                                'test', 'agent:test', 'pending')
                        """,
                        (proposal_id, payload),
                    )
                connection.commit()
                accepted.append(label)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM vault.vault_amendment_proposals WHERE id = %s",
                        (proposal_id,),
                    )
                connection.commit()
            except psycopg.errors.CheckViolation:
                connection.rollback()
                refused.append(label)
    finally:
        connection.close()

    assert accepted == ["related_ids only", "facets only", "cleared source_url"], (
        "The constraint stopped accepting a payload the metadata kind needs. "
        "Widening the CHECK is the right fix only if the key is genuinely "
        "excluded from `assemble_embedding_text` -- confirm that first, in a "
        "migration, rather than here."
    )
    assert refused == [
        "tags (embedded)",
        "aliases (embedded)",
        "body smuggled in",
        "empty object",
    ], (
        "The constraint accepted a payload it must refuse. Do not move the "
        "case into the accepted list to make this pass: that list is the "
        "guarantee, not a description of current behaviour. `tags` and "
        "`aliases` join the embedding text, so admitting either lets a "
        "'metadata' edit change what a note means to search while skipping "
        "the re-embed and the dedup gate it would need -- and a smuggled "
        "`body` is a content rewrite wearing the cheap kind's name. The fix "
        "is a migration that restores the CHECK."
    )


def test_both_scopes_get_a_metadata_path_and_neither_can_reach_content() -> None:
    """Propose and update each need one, or the wrong caller is stuck.

    Without the update-scope tool, the holder of the *stronger* scope has only
    the sharper instrument for this job: `vault_update_note` is a full
    replacement, so adding one edge means resending the body. A capability that
    can replace a note should be able to do the narrower thing directly.

    Review needs no tool of its own — `vault_decide_amendment_proposal` is
    kind-agnostic and materialises a metadata proposal through the same
    `_update_request` branch — but it does need to render one, which
    `test_a_stored_metadata_proposal_renders_as_a_metadata_change` covers.
    """

    from app.vault.mcp import _TOOL_SCOPES, build_vault_mcp_server

    assert _TOOL_SCOPES["vault_propose_note_metadata"][0] == "vault:propose"
    assert _TOOL_SCOPES["vault_update_note_metadata"][0] == "vault:update"

    tools = {t.name: t for t in build_vault_mcp_server()._tool_manager.list_tools()}
    for name in ("vault_propose_note_metadata", "vault_update_note_metadata"):
        properties = set(tools[name].parameters["properties"])
        # The payload is the guarantee. Anything embedded being absent here is
        # what lets both tools skip the re-embed and the dedup gate.
        assert {"title", "body", "tags", "aliases", "summary"} & properties == set()
        assert {"related_ids", "source_ids", "facets"} <= properties
        assert "base_revision" in properties, name
