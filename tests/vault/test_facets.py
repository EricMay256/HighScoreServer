"""Facet normalization, vocabulary, and the exclusion that justifies the column.

The load-bearing test here is
``test_facets_never_reach_the_embedding_text``. ADR 0017 exists because a shared
value in an embedded field inflates pairwise cosine by roughly 0.04 against a
dedup margin of 0.0094, so "facets are not embedded" is not tidiness -- it is
what keeps ``flag_at`` calibratable at all.
"""

import pytest

from app.vault.constants import MAX_FACET_NAME_LENGTH, MAX_FACET_VALUE_LENGTH
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.embedding_text import assemble_embedding_text, digest_for
from app.vault.facets import (
    FACET_NAMES,
    MAX_FACETS_PER_DOCUMENT,
    MAX_VALUES_PER_FACET,
    normalize_facets,
    validate_facets,
)


def make_document(**overrides) -> NewVaultDocument:
    defaults = {
        "id": "facet-doc",
        "kind": DocumentKind.NOTE,
        "doc_type": "Agent Note",
        "vault_path": "Agent/notes/facet-doc.md",
        "status": DocumentStatus.ACTIVE,
        "doc_status": "Active",
        "title": "Package cache expansion",
        "body": "Unity keeps an embedded package's imported state per editor.",
        "tags": ("unity", "gotcha"),
        "contributed_by": "agent:test",
        "provenance": {},
    }
    defaults.update(overrides)
    return NewVaultDocument(**defaults)


class TestFacetsAreNotEmbedded:
    def test_facets_never_reach_the_embedding_text(self) -> None:
        """The whole reason facets are a column and not namespaced tags.

        Measured: one shared tag raised every pairwise cosine in a 10-document
        sample, mean +0.0385, against a floor-to-ceiling dedup margin of
        0.0094. If a facet ever entered this text, that inflation would come
        with it and the gate would stop being calibratable.
        """

        without = make_document()
        with_facets = make_document(
            facets={"project": ["highscoreserver"], "area": ["backend"]}
        )

        assert assemble_embedding_text(without) == assemble_embedding_text(
            with_facets
        )

    def test_adding_a_facet_does_not_change_the_embedding_digest(self) -> None:
        """So adding a project to an existing note costs no re-embed.

        A pleasant consequence rather than the goal: `embedded_text_sha256`
        decides staleness (ADR 0013), and classification is not a change in
        meaning.
        """

        assert digest_for(make_document()) == digest_for(
            make_document(facets={"project": ["highscoreserver"]})
        )

    def test_a_tag_by_contrast_does_change_the_digest(self) -> None:
        """The contrast that makes the previous test meaningful."""

        assert digest_for(make_document()) != digest_for(
            make_document(tags=("unity", "gotcha", "highscoreserver"))
        )


class TestNormalizeFacets:
    def test_values_are_stripped_deduplicated_and_sorted(self) -> None:
        assert normalize_facets(
            {"project": ["  beta ", "alpha", "beta", "alpha  "]}
        ) == {"project": ["alpha", "beta"]}

    def test_a_facet_whose_values_all_vanish_is_dropped(self) -> None:
        """An empty array and an absent key would otherwise be two spellings of
        the same thing that a containment query treats differently."""

        assert normalize_facets({"project": ["", "   "]}) == {}

    def test_facet_names_are_stripped(self) -> None:
        assert normalize_facets({"  project  ": ["hss"]}) == {"project": ["hss"]}

    def test_an_empty_map_stays_empty(self) -> None:
        assert normalize_facets({}) == {}


class TestValidateFacets:
    def test_a_known_facet_is_valid(self) -> None:
        assert validate_facets({"project": ["highscoreserver"]}) == []

    def test_an_unknown_facet_name_is_refused_and_lists_the_known_ones(
        self,
    ) -> None:
        """A typo like 'projects' would otherwise file a note where nothing
        looks for it, with no error anywhere."""

        errors = validate_facets({"projects": ["hss"]})

        assert len(errors) == 1
        assert "projects" in errors[0]
        for name in FACET_NAMES:
            assert name in errors[0]

    def test_every_declared_facet_name_validates(self) -> None:
        for name in FACET_NAMES:
            assert validate_facets({name: ["value"]}) == []

    def test_too_many_values_in_one_facet_is_refused(self) -> None:
        errors = validate_facets(
            {"project": [f"p{index}" for index in range(MAX_VALUES_PER_FACET + 1)]}
        )

        assert any("values" in error for error in errors)

    def test_too_many_facets_is_refused(self) -> None:
        facets = {
            f"facet{index}": ["v"] for index in range(MAX_FACETS_PER_DOCUMENT + 1)
        }

        assert any("too many facets" in error for error in validate_facets(facets))

    def test_an_overlong_value_is_refused(self) -> None:
        errors = validate_facets({"project": ["x" * (MAX_FACET_VALUE_LENGTH + 1)]})

        assert any("exceeds" in error for error in errors)

    def test_errors_accumulate_rather_than_stopping_at_the_first(self) -> None:
        """Mirrors governance.validate: one round trip, everything wrong."""

        errors = validate_facets(
            {
                "nonsense": ["a"],
                "alsobad": ["b"],
            }
        )

        assert len(errors) >= 2

    @pytest.mark.parametrize("name", ["project", "area", "system"])
    def test_the_documented_facet_names_are_the_ones_implemented(
        self, name: str
    ) -> None:
        assert name in FACET_NAMES

    def test_the_name_ceiling_matches_the_database_constraint(self) -> None:
        """Migration 0005 restates 64 in its CHECK. If this constant moves and
        the migration does not, the database silently becomes the stricter of
        the two."""

        assert MAX_FACET_NAME_LENGTH == 64
