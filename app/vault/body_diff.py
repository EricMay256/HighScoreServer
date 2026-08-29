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


class SpanEditError(BodyDiffError):
    """The named span does not identify exactly one place in the body.

    A subclass of ``BodyDiffError`` so every caller that already renders a
    malformed diff as a client error renders this the same way. It is a
    distinct type because the *remedy* differs: a bad diff is re-authored,
    while an ambiguous span is re-pointed with ``occurrence``.
    """


def apply_span_edit(
    original: str,
    *,
    expected_text: str,
    replacement_text: str,
    occurrence: int | None = None,
) -> str:
    """Replace one exactly-matched span, or refuse to guess which one.

    The alternative to authoring a unified diff. Hunk arithmetic is the part
    of a patch a model gets wrong -- line numbers and counts it cannot verify
    without holding the whole file -- and it is also the part that carries no
    intent. Naming the old text and the new text states the intent exactly and
    lets the server compute the arithmetic, which it can do correctly by
    construction.

    **This does not relax the diff contract; it feeds it.** The caller's span
    is converted to a canonical unified diff and then applied through
    ``apply_body_unified_diff`` like any other, so the compact-diff policy,
    the removal-acknowledgement rule and the context checks all still apply.
    Nothing here reinterprets a malformed patch -- a malformed patch never
    arrives.

    ``occurrence`` is deliberately ``None`` rather than ``1`` by default.
    Defaulting to the first match would make "the span I meant" and "the first
    span that happens to match" the same request, and the caller could not
    express the difference. ``None`` means *this text must be unique*, which
    is the safe reading of a caller who has not thought about duplicates; an
    explicit integer is the caller saying they have.

    Raises ``SpanEditError`` when the span is absent, ambiguous, or out of
    range, and ``BodyDiffError`` when the edit would change nothing.
    """

    if not expected_text:
        raise SpanEditError("expected_text must not be empty")

    # The body is the stored one and decides the line-ending convention; the
    # caller's span is normalized to match rather than the other way round.
    # `apply_body_unified_diff` refuses a body with mixed endings, so a body
    # that reaches here is uniform and one normalization is enough.
    body = original.replace("\r\n", "\n")
    needle = expected_text.replace("\r\n", "\n")
    replacement = replacement_text.replace("\r\n", "\n")

    # CRLF is normalized above because the stored body decides the convention.
    # A carriage return that survives that is a *lone* one, and it is not
    # representable: the diff is built with `splitlines`, which treats a bare
    # \r as a line boundary, so the stored patch would apply "x\ny" for a
    # requested "x\ry" -- a proposal that silently describes different text
    # from the one the caller asked for.
    for label, value in (("expected_text", needle), ("replacement_text", replacement)):
        if "\r" in value:
            raise SpanEditError(
                f"{label} contains a carriage return that is not part of a "
                "CRLF line ending. The stored diff cannot represent it. Use "
                "\\n line endings."
            )

    if needle == replacement:
        raise BodyDiffError("expected_text and replacement_text are identical")

    # Every offset the span begins at, **including overlapping ones**, and the
    # same list answers both "is this ambiguous" and "which one did you mean".
    #
    # Two different overlap policies used to be in play here: `str.count`
    # counts non-overlapping occurrences, while stepping with
    # `index(needle, previous + 1)` finds overlapping ones. For "aa" in "aaa"
    # that meant the count said 1 and the ambiguity check passed, so the edit
    # landed on the first of two candidate spans without asking -- the one
    # thing ADR 0033 says this must never do. Overlapping is the safer reading
    # of "every place this span begins": it can only ever refuse more.
    offsets: list[int] = []
    search_from = 0
    while (found := body.find(needle, search_from)) != -1:
        offsets.append(found)
        search_from = found + 1

    count = len(offsets)
    if count == 0:
        raise SpanEditError(
            "expected_text does not appear in the note body; fetch the note "
            "again and copy the span exactly, whitespace included"
        )

    if occurrence is None:
        if count > 1:
            raise SpanEditError(
                f"expected_text appears {count} times; pass occurrence to say "
                "which one, or extend the span until it is unique"
            )
        index = offsets[0]
    else:
        if not 1 <= occurrence <= count:
            raise SpanEditError(
                f"occurrence {occurrence} is out of range; expected_text "
                f"appears {count} time{'s' if count != 1 else ''}"
            )
        index = offsets[occurrence - 1]

    return body[:index] + replacement + body[index + len(needle) :]


def span_edit_to_unified_diff(
    original: str,
    *,
    expected_text: str,
    replacement_text: str,
    occurrence: int | None = None,
) -> str:
    """Render a span edit as the canonical diff that will be stored and reviewed.

    Storage stays a body diff, which is the point: a reviewer reads the same
    artifact whichever way it was authored, the proposal table needs no new
    kind, and no migration is involved. The span is a transport convenience
    that ends at this function.
    """

    body = original.replace("\r\n", "\n")
    updated = apply_span_edit(
        original,
        expected_text=expected_text,
        replacement_text=replacement_text,
        occurrence=occurrence,
    )

    # The diff grammar has no `\ No newline at end of file` marker, and the
    # applier keeps the original body's trailing-newline state, so an edit that
    # adds or removes the final newline cannot be carried by the artifact that
    # gets stored. Refused here rather than accepted and quietly dropped.
    if body.endswith("\n") != updated.endswith("\n"):
        raise SpanEditError(
            "this edit changes whether the body ends with a newline, which the "
            "stored diff cannot represent. Propose it as a replacement instead."
        )

    diff = summarize_body_change(body, updated).unified_diff

    # The invariant the whole feature rests on: what is stored has to apply to
    # exactly the text that was asked for. Everything above refuses a known
    # unrepresentable input; this catches the ones nobody has thought of yet,
    # by round-tripping the artifact before it is persisted rather than
    # discovering the difference at review time when the patch is applied.
    applied = apply_body_unified_diff(body, diff)
    if applied.body != updated:
        raise SpanEditError(
            "the diff generated for this span does not reproduce the requested "
            "text, so it was not stored. Propose the change as a replacement."
        )
    return diff
