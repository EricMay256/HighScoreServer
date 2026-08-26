"""Strict body-diff application and reviewer-facing change summaries."""

import difflib
import math
import re
from dataclasses import dataclass


MAX_BODY_DIFF_CHARS = 50_000
MAX_BODY_DIFF_HUNKS = 20
MAX_BODY_DIFF_CHANGED_LINES = 200
MAX_BODY_DIFF_CHANGED_RATIO = 0.25
MIN_BODY_DIFF_CHANGED_LINE_ALLOWANCE = 20


class BodyDiffError(ValueError):
    """The proposed diff is malformed, stale, or exceeds the compact-diff policy."""


@dataclass(frozen=True, slots=True)
class RemovedBodyLine:
    line_number: int
    text: str


@dataclass(frozen=True, slots=True)
class BodyDiffResult:
    body: str
    added_line_count: int
    removed_lines: tuple[RemovedBodyLine, ...]
    hunk_count: int


@dataclass(frozen=True, slots=True)
class BodyChangeSummary:
    resulting_body: str
    unified_diff: str
    added_line_count: int
    removed_lines: tuple[RemovedBodyLine, ...]
    hunk_count: int

    @property
    def requires_removal_acknowledgement(self) -> bool:
        return bool(self.removed_lines)


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)


def apply_body_unified_diff(original: str, patch: str) -> BodyDiffResult:
    """Apply a bounded unified diff to an exact body without invoking a shell."""

    if len(patch) > MAX_BODY_DIFF_CHARS:
        raise BodyDiffError(
            f"body_diff exceeds the {MAX_BODY_DIFF_CHARS}-character limit; "
            "use a full replacement"
        )
    normalized_patch = patch.replace("\r\n", "\n")
    if "\r" in normalized_patch:
        raise BodyDiffError("body_diff contains an unsupported carriage return")
    patch_lines = normalized_patch.splitlines()
    if not patch_lines:
        raise BodyDiffError("body_diff must contain at least one hunk")

    index = 0
    if patch_lines[index].startswith("--- "):
        index += 1
        if index >= len(patch_lines) or not patch_lines[index].startswith("+++ "):
            raise BodyDiffError("a --- header must be followed by a +++ header")
        index += 1

    without_crlf = original.replace("\r\n", "")
    if "\r" in without_crlf:
        raise BodyDiffError("note body contains unsupported mixed line endings")
    line_ending = "\r\n" if "\r\n" in original else "\n"
    original_lines = original.replace("\r\n", "\n").splitlines()
    trailing_newline = original.endswith(line_ending)
    output: list[str] = []
    removed: list[RemovedBodyLine] = []
    original_cursor = 0
    additions = 0
    hunks = 0

    while index < len(patch_lines):
        match = _HUNK_HEADER.fullmatch(patch_lines[index])
        if match is None:
            raise BodyDiffError("expected a unified-diff hunk header")
        hunks += 1
        if hunks > MAX_BODY_DIFF_HUNKS:
            raise BodyDiffError(
                f"body_diff exceeds the {MAX_BODY_DIFF_HUNKS}-hunk limit; "
                "use a full replacement"
            )
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if old_start < 1 or old_count < 1:
            raise BodyDiffError(
                "each hunk must identify existing context or removed text"
            )
        hunk_start = old_start - 1
        if hunk_start < original_cursor or hunk_start > len(original_lines):
            raise BodyDiffError("hunks overlap or fall outside the note body")
        output.extend(original_lines[original_cursor:hunk_start])
        expected_new_start = len(output) if new_count == 0 else len(output) + 1
        if new_start != expected_new_start:
            raise BodyDiffError("body_diff new-file coordinates are inconsistent")
        source_index = hunk_start
        index += 1
        seen_old = 0
        seen_new = 0
        seen_anchor = False

        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if not line:
                raise BodyDiffError("every hunk line needs a diff prefix")
            if line.startswith("\\"):
                raise BodyDiffError("no-newline markers are not supported")
            if line[0] not in {" ", "+", "-"}:
                raise BodyDiffError("hunk lines must be context, additions, or removals")
            value = line[1:]
            if line[0] in {" ", "-"}:
                if (
                    source_index >= len(original_lines)
                    or original_lines[source_index] != value
                ):
                    raise BodyDiffError("body_diff context does not match the note body")
                seen_anchor = True
                seen_old += 1
                if line[0] == " ":
                    output.append(value)
                    seen_new += 1
                else:
                    removed.append(
                        RemovedBodyLine(line_number=source_index + 1, text=value)
                    )
                source_index += 1
            else:
                output.append(value)
                additions += 1
                seen_new += 1
            index += 1

        if seen_old != old_count or seen_new != new_count:
            raise BodyDiffError("body_diff hunk counts do not match its lines")
        if not seen_anchor:
            raise BodyDiffError("each hunk needs exact context or removed text")
        original_cursor = source_index

    if hunks == 0 or (additions == 0 and not removed):
        raise BodyDiffError("body_diff must change at least one line")
    change_extent = max(additions, len(removed))
    proportional_allowance = math.ceil(
        len(original_lines) * MAX_BODY_DIFF_CHANGED_RATIO
    )
    allowed_lines = min(
        MAX_BODY_DIFF_CHANGED_LINES,
        max(MIN_BODY_DIFF_CHANGED_LINE_ALLOWANCE, proportional_allowance),
    )
    if change_extent > allowed_lines:
        raise BodyDiffError(
            f"body_diff changes {change_extent} lines; the compact-diff limit for "
            f"this note is {allowed_lines}. Use a full replacement"
        )

    output.extend(original_lines[original_cursor:])
    body = line_ending.join(output)
    if trailing_newline:
        body += line_ending
    return BodyDiffResult(body, additions, tuple(removed), hunks)


def summarize_body_change(original: str, updated: str) -> BodyChangeSummary:
    """Materialize a full, deletion-aware review summary for any body change."""

    original_lines = original.replace("\r\n", "\n").splitlines()
    updated_lines = updated.replace("\r\n", "\n").splitlines()
    matcher = difflib.SequenceMatcher(a=original_lines, b=updated_lines, autojunk=False)
    removed: list[RemovedBodyLine] = []
    additions = 0
    hunks = 0
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks += 1
        if tag in {"delete", "replace"}:
            removed.extend(
                RemovedBodyLine(line_number=index + 1, text=original_lines[index])
                for index in range(old_start, old_end)
            )
        if tag in {"insert", "replace"}:
            additions += new_end - new_start
    rendered_diff = "\n".join(
        difflib.unified_diff(
            original_lines,
            updated_lines,
            fromfile="current-body",
            tofile="proposed-body",
            lineterm="",
        )
    )
    return BodyChangeSummary(
        resulting_body=updated,
        unified_diff=rendered_diff,
        added_line_count=additions,
        removed_lines=tuple(removed),
        hunk_count=hunks,
    )
