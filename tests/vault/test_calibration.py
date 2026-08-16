"""The two-sided ``flag_at`` derivation.

These tests are about the *procedure*, not about any particular threshold. The
values in the model register come from a paid API and a populated corpus, so
they are measured by ``scripts/measure_dedup_similarity.py`` rather than
asserted here. What is asserted is that the derivation refuses to produce a
threshold it cannot justify — which is the part that nearly went wrong.
"""

import pytest

from app.vault.calibration import (
    MINIMUM_SEPARATION,
    REFERENCE_DUPLICATE_PAIRS,
    derive_flag_at,
)
from app.vault.domain import DocumentKind, DocumentStatus, NewVaultDocument
from app.vault.embedding_text import EmbeddableDocument, assemble_embedding_text
from app.vault.governance import DEFAULT_POLICY, Policy, ScoredCandidate, decide


def make_candidate() -> NewVaultDocument:
    return NewVaultDocument(
        id="calibration-candidate",
        kind=DocumentKind.NOTE,
        doc_type="Agent Note",
        vault_path="Agent/notes/calibration-candidate.md",
        status=DocumentStatus.ACTIVE,
        doc_status="Active",
        title="A candidate",
        body="Body text.",
        tags=[],
        contributed_by="agent:test",
        source_url=None,
        provenance={},
    )


class TestReferencePairs:
    """The positive control: what a correctly calibrated gate must catch."""

    def test_every_pair_states_the_same_insight_twice_without_repeating_itself(
        self,
    ) -> None:
        """Not byte-equal, and not a near string match either.

        A pair that shared most of its wording would be caught by the Stage-A
        string deduper and would measure nothing new. The point of these pairs
        is that only a semantic model can tell they are duplicates.
        """

        for index, (left, right) in enumerate(REFERENCE_DUPLICATE_PAIRS):
            assert left.body != right.body, f"pair {index} is byte-equal"

            left_words = set(left.body.lower().split())
            right_words = set(right.body.lower().split())
            overlap = len(left_words & right_words) / len(left_words | right_words)
            assert overlap < 0.5, (
                f"pair {index} shares {overlap:.0%} of its vocabulary — a string "
                "deduper would catch this, so it does not test semantic dedup"
            )

    def test_there_is_more_than_one_pair(self) -> None:
        """The ceiling is a minimum over these, so a single pair is a single
        point of failure for the whole true-positive side."""

        assert len(REFERENCE_DUPLICATE_PAIRS) >= 3

    def test_reference_notes_are_note_shaped_not_bare_prose(self) -> None:
        """The fix for the measurement bias described in calibration.py.

        The corpus floor comes from vectors built by assemble_embedding_text
        over title + aliases + tags + summary + body. A ceiling measured on
        bodies alone is not comparable to it: tags alone move the maximum
        corpus pair by roughly 0.05, so the margin would be understated and the
        derivation biased toward "not separable".
        """

        for index, pair in enumerate(REFERENCE_DUPLICATE_PAIRS):
            for note in pair:
                assert note.title.strip(), f"pair {index} has an untitled side"
                assert note.tags, f"pair {index} has an untagged side"

    def test_reference_notes_satisfy_the_embeddable_protocol(self) -> None:
        """So both sides of the derivation can run through one assembler."""

        for left, right in REFERENCE_DUPLICATE_PAIRS:
            for note in (left, right):
                assert isinstance(note, EmbeddableDocument)
                assert assemble_embedding_text(note)

    def test_pair_tags_overlap_without_being_identical(self) -> None:
        """Mirrors the real corpus: two notes restating one insight would be
        filed similarly but not by the same hand. Identical tags would inflate
        the ceiling and flatter the result."""

        for index, (left, right) in enumerate(REFERENCE_DUPLICATE_PAIRS):
            shared = set(left.tags) & set(right.tags)
            assert shared, f"pair {index} shares no tags"
            assert set(left.tags) != set(right.tags), (
                f"pair {index} has identical tags, which overstates the ceiling"
            )

    def test_the_title_and_tags_actually_reach_the_assembled_text(self) -> None:
        """Guards the wiring: if assemble_embedding_text stopped including
        tags, this fixture would silently become bare prose again."""

        note = REFERENCE_DUPLICATE_PAIRS[0][0]
        assembled = assemble_embedding_text(note)

        assert note.title in assembled
        for tag in note.tags:
            assert tag in assembled


class TestDeriveFlagAt:
    def test_clean_separation_recommends_a_value_inside_the_gap(self) -> None:
        bands = derive_flag_at(
            distinct_scores=[0.30, 0.55, 0.60],
            duplicate_scores=[0.90, 0.95],
        )

        assert bands.separated
        assert bands.recommended is not None
        assert bands.floor < bands.recommended <= bands.ceiling

    def test_overlapping_populations_recommend_nothing(self) -> None:
        """A distinct pair scoring above a duplicate pair means the model cannot
        tell them apart. There is no correct threshold, not even a bad one."""

        bands = derive_flag_at(
            distinct_scores=[0.95],
            duplicate_scores=[0.80],
        )

        assert not bands.separated
        assert bands.recommended is None
        assert "overlap" in bands.reason

    def test_a_gap_narrower_than_the_minimum_recommends_nothing(self) -> None:
        """The measured `text-embedding-3-small` case: floor 0.7406, ceiling
        0.7500. The populations technically separate but only by noise."""

        bands = derive_flag_at(
            distinct_scores=[0.7406],
            duplicate_scores=[0.7500, 0.7664, 0.8431],
        )

        assert bands.floor == pytest.approx(0.7406)
        assert bands.ceiling == pytest.approx(0.7500)
        assert not bands.separated
        assert bands.recommended is None

    def test_a_narrow_gap_never_recommends_a_value_below_its_own_floor(self) -> None:
        """Regression: rounding the midpoint of a narrow gap to two decimals can
        land *under* the floor.

        The pre-correction measurement (floor 0.7406, ceiling 0.7478) has a
        midpoint of 0.74415, which rounds to 0.74 — below the floor, and
        therefore a threshold that flags the very pair proving it is too low.
        Those values are kept rather than updated to the corrected 0.7500
        ceiling precisely because they still demonstrate the arithmetic hazard;
        the corrected pair happens to round safely, which would make this test
        prove nothing. The separation guard rejects this band before rounding is
        reached, and this test fails if that ordering is ever inverted.
        """

        bands = derive_flag_at(
            distinct_scores=[0.7406],
            duplicate_scores=[0.7478],
        )

        assert bands.recommended is None or bands.recommended > bands.floor

    def test_no_duplicate_measurements_means_no_recommendation(self) -> None:
        """Corpus-only measurement is exactly the mistake the procedure exists
        to prevent: it looks like a wide safe band and is not one."""

        bands = derive_flag_at(
            distinct_scores=[0.10, 0.40, 0.7406],
            duplicate_scores=[],
        )

        assert not bands.separated
        assert bands.recommended is None

    @pytest.mark.parametrize("gap", [0.0, 0.001, MINIMUM_SEPARATION / 2])
    def test_insufficient_gaps_are_all_refused(self, gap: float) -> None:
        bands = derive_flag_at(
            distinct_scores=[0.70],
            duplicate_scores=[0.70 + gap],
        )

        assert bands.recommended is None

    def test_recommendation_is_quoted_to_two_decimals(self) -> None:
        """Embedding endpoints are not bit-deterministic and both bounds are
        sample extremes, so a threshold with more precision than this is
        claiming something the measurement does not support."""

        bands = derive_flag_at(
            distinct_scores=[0.612345],
            duplicate_scores=[0.887654],
        )

        assert bands.recommended is not None
        assert bands.recommended == round(bands.recommended, 2)


class TestDefaultPolicyMatchesTheRegister:
    def test_default_flag_at_is_one_until_a_model_is_calibrated(self) -> None:
        """`text-embedding-3-small` measured as unseparated, so 1.0 stands.

        Changing this constant requires a row in the model register in
        docs/embedding-calibration.md. If this test is failing because you
        lowered the default, add the measurement that justifies it.
        """

        assert DEFAULT_POLICY.flag_at == 1.0

    def test_at_one_point_zero_a_near_duplicate_still_inserts(self) -> None:
        """The consequence of the measured verdict, stated as behavior.

        The strongest reference pair scores ~0.81. Under the current policy that
        is an ordinary insert, not a flag — semantic dedup is genuinely not
        catching restatements today, and that is a deliberate, measured position
        rather than an oversight.
        """

        action = decide(
            make_candidate(),
            [ScoredCandidate(note_id="existing", title="Existing", score=0.8085)],
            DEFAULT_POLICY,
        )

        assert type(action).__name__ == "Insert"

    def test_an_identical_embedding_still_flags(self) -> None:
        """1.0 is dedup narrowed, not dedup disabled."""

        action = decide(
            make_candidate(),
            [ScoredCandidate(note_id="existing", title="Existing", score=1.0)],
            DEFAULT_POLICY,
        )

        assert type(action).__name__ == "Flag"

    def test_a_calibrated_policy_would_flag_the_reference_band(self) -> None:
        """Guards the wiring rather than the constant: if a future model does
        separate, setting flag_at is all that is needed."""

        action = decide(
            make_candidate(),
            [ScoredCandidate(note_id="existing", title="Existing", score=0.8085)],
            Policy(flag_at=0.75),
        )

        assert type(action).__name__ == "Flag"
