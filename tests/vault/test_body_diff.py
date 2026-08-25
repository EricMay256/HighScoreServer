"""Structural and review guarantees for compact body diffs."""

import pytest

from app.vault.body_diff import (
    BodyDiffError,
    apply_body_unified_diff,
    summarize_body_change,
)


def test_applies_additions_edits_and_removals() -> None:
    original = "alpha\nbeta\ngamma\n"
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        "-gamma\n"
        "+delta\n"
    )

    result = apply_body_unified_diff(original, patch)

    assert result.body == "alpha\nBETA\ndelta\n"
    assert result.added_line_count == 2
    assert [(item.line_number, item.text) for item in result.removed_lines] == [
        (2, "beta"),
        (3, "gamma"),
    ]
    assert result.hunk_count == 1


def test_accepts_a_deletion_hunk_anchored_by_the_removed_text() -> None:
    result = apply_body_unified_diff(
        "alpha\nbeta\n",
        "@@ -2 +1,0 @@\n-beta",
    )

    assert result.body == "alpha\n"
    assert result.removed_lines[0].line_number == 2


def test_applies_multiple_non_overlapping_hunks_with_new_coordinates() -> None:
    result = apply_body_unified_diff(
        "alpha\nbeta\ngamma\n",
        (
            "--- current-body\n"
            "+++ proposed-body\n"
            "@@ -1,2 +1,3 @@\n"
            " alpha\n"
            "+between\n"
            " beta\n"
            "@@ -3 +4,2 @@\n"
            " gamma\n"
            "+after"
        ),
    )

    assert result.body == "alpha\nbetween\nbeta\ngamma\nafter\n"
    assert result.hunk_count == 2


def test_preserves_crlf_line_endings() -> None:
    result = apply_body_unified_diff(
        "alpha\r\nbeta\r\n",
        "@@ -1,2 +1,3 @@\n alpha\n+between\n beta",
    )

    assert result.body == "alpha\r\nbetween\r\nbeta\r\n"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("@@ -1 +1,2 @@\n wrong\n+new", "does not match"),
        ("@@ -1,2 +1,2 @@\n alpha\n+new", "counts"),
        ("@@ -1 +2,2 @@\n alpha\n+new", "coordinates"),
        ("@@ -1 +1 @@\n alpha", "must change"),
        ("@@ -1,0 +1 @@\n+new", "existing context"),
    ],
)
def test_rejects_malformed_or_unanchored_diffs(patch: str, message: str) -> None:
    with pytest.raises(BodyDiffError, match=message):
        apply_body_unified_diff("alpha\n", patch)


def test_refuses_a_large_diff_in_favor_of_full_replacement() -> None:
    original_lines = [f"line-{index}" for index in range(100)]
    patch_lines = ["@@ -1,26 +1,26 @@"]
    patch_lines.extend(f"-{line}" for line in original_lines[:26])
    patch_lines.extend(f"+changed-{index}" for index in range(26))

    with pytest.raises(BodyDiffError, match="compact-diff limit"):
        apply_body_unified_diff("\n".join(original_lines), "\n".join(patch_lines))


def test_review_summary_makes_removed_lines_explicit() -> None:
    summary = summarize_body_change("safe\nwarning\n", "safe\nupdated\n")

    assert summary.resulting_body == "safe\nupdated\n"
    assert summary.added_line_count == 1
    assert [(line.line_number, line.text) for line in summary.removed_lines] == [
        (2, "warning")
    ]
    assert summary.requires_removal_acknowledgement is True
    assert "-warning" in summary.unified_diff
    assert "+updated" in summary.unified_diff
