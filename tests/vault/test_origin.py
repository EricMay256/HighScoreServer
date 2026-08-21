"""Upstream provenance: the closed key set, the shape rules, and the digest.

``origin`` exists because the 2026-08-12 corpus import had nowhere to put the
authoring agent, the authoring time, a prose ``Source``, or a ``ClientRunID``,
and dropped all four. These are the rules that stop the next import doing the
same thing, plus the one property that made the field cheap to add.
"""

import pytest

from app.vault.api_models import VaultContributionRequest, canonical_request_digest
from app.vault.origin import ORIGIN_KEYS, normalize_origin, validate_origin


def _payload(**overrides: object) -> dict:
    return {
        "title": "A note with a life before this vault",
        "body": "Replayed from the Stage-A corpus.",
        "idempotency_key": "origin-test-key",
        **overrides,
    }


def test_the_key_set_covers_what_the_import_dropped() -> None:
    assert ORIGIN_KEYS == {
        "author",
        "created_at",
        "updated_at",
        "reference",
        "run_id",
    }


def test_blank_values_are_dropped_rather_than_stored() -> None:
    """A key present but blank and a key absent are both "not stated"."""

    assert normalize_origin(
        {"author": "  agent:codex  ", "reference": "   ", "run_id": ""}
    ) == {"author": "agent:codex"}


def test_an_unknown_field_is_refused_with_the_known_set() -> None:
    errors = validate_origin({"authour": "agent:codex"})

    assert len(errors) == 1
    assert "authour" in errors[0]
    assert "author" in errors[0]


def test_every_problem_is_reported_at_once() -> None:
    """Mirrors validate_facets: one round trip, not one problem per round trip."""

    errors = validate_origin(
        {"nonsense": "x", "created_at": "yesterday", "updated_at": "also not a date"}
    )

    assert len(errors) == 3


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-30T18:54:39Z",
        "2026-07-30T18:54:39.123456Z",
        "2026-07-30T18:54:39+00:00",
        "2026-07-30 18:54:39-07:00",
    ],
)
def test_iso_8601_timestamps_with_an_offset_are_accepted(value: str) -> None:
    assert validate_origin({"created_at": value}) == []


@pytest.mark.parametrize(
    "value",
    [
        # A bare date says nothing about the time.
        "2026-07-30",
        # No offset is ambiguous by exactly the amount that made the exporter's
        # UTC normalization necessary in the first place.
        "2026-07-30T18:54:39",
        "30 July 2026",
        "",
    ],
)
def test_a_timestamp_without_an_offset_is_refused(value: str) -> None:
    assert validate_origin({"created_at": value}) != []


def test_an_over_long_value_is_refused() -> None:
    assert validate_origin({"reference": "x" * 257}) != []


def test_adding_origin_did_not_change_any_existing_digest() -> None:
    """The reason this field needed no REQUEST_DIGEST_VERSION bump.

    ``canonical_request_digest`` dumps with ``exclude_unset=True`` (migration
    0006, ADR 0016's amendment) precisely so an additive optional field is a
    non-event. These digests were computed before ``origin`` existed; if this
    test fails, every stored digest has been invalidated and the version must be
    bumped.
    """

    assert (
        canonical_request_digest(
            VaultContributionRequest.model_validate(
                {"title": "T", "body": "B", "idempotency_key": "k-12345678"}
            )
        ).hex()
        == "b1c2a2b283bc0efc50bba128402afe834226024d4bea0ceadd3f256d7541e579"
    )
    assert (
        canonical_request_digest(
            VaultContributionRequest.model_validate(
                {
                    "title": "T",
                    "body": "B",
                    "tags": ["a"],
                    "facets": {"project": ["hss"]},
                    "idempotency_key": "k-12345678",
                }
            )
        ).hex()
        == "c51b521204c5a58c6eba2fd9a315232677371e20ed0edd4d207d692c163fabcb"
    )


def test_a_request_that_sets_origin_gets_a_different_digest() -> None:
    """The other half: origin is part of the request, so two requests differing
    only in origin are different requests and must not replay each other."""

    plain = canonical_request_digest(
        VaultContributionRequest.model_validate(_payload())
    )
    with_origin = canonical_request_digest(
        VaultContributionRequest.model_validate(
            _payload(origin={"author": "agent:codex"})
        )
    )

    assert plain != with_origin


def test_origin_is_contribution_only() -> None:
    """An update is a new body for an existing row, not a new provenance for it.

    Where the content came from does not change because someone edited it, and
    the update model forbids extras, so this is a 422 rather than a silent
    no-op.
    """

    from app.vault.api_models import VaultDocumentUpdateRequest

    with pytest.raises(ValueError):
        VaultDocumentUpdateRequest.model_validate(
            {
                "title": "T",
                "body": "B",
                "origin": {"author": "agent:codex"},
            }
        )
