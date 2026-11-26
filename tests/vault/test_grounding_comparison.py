"""Offline tests for comparison recording; never launch a model or use quota."""

import hashlib
import json
import unittest
from pathlib import Path

from scripts.evaluate_vault_grounding import DEFAULT_FIXTURES, Call, Corpus, read_json
from scripts.run_vault_grounding_comparison import (
    DISABLED_FEATURES,
    case_prompt,
    normalize,
    verify_call_recording,
)


class GroundingComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = Corpus.model_validate(read_json(DEFAULT_FIXTURES)).cases[0]
        self.entry = "[V1] Batch policy — note ID: 00000000000000000000000000000001; revision: 1."

    def test_normalization_preserves_answer_verbatim(self) -> None:
        final = "25 sources [V1].\n\n" + self.entry + "\n"
        answer = normalize(self.case, [], final, [])
        self.assertEqual(answer.final_answer, final)
        self.assertEqual(answer.citations[0].label, "V1")
        self.assertEqual(answer.citations[0].content_revision, 1)
        self.assertIsNone(answer.citations[0].excerpt)

    def test_normalizer_does_not_repair_bad_source_format(self) -> None:
        for final in (
            "[V1] Batch policy",
            "**" + self.entry + "**",
            self.entry.replace("revision: 1", "revision: unknown"),
        ):
            with self.subTest(final=final):
                answer = normalize(self.case, [], final, [])
                self.assertEqual(answer.citations, [])
                self.assertEqual(answer.final_answer, final)

    def test_duplicate_entries_are_not_silently_deduplicated(self) -> None:
        answer = normalize(self.case, [], self.entry + "\n" + self.entry, [])
        self.assertEqual(len(answer.citations), 2)

    def test_nonfixture_tools_remain_visible_to_checker(self) -> None:
        for kind in ("command_execution", "file_change", "web_search"):
            with self.subTest(kind=kind):
                events = [{"type": "item.completed", "item": {"type": kind}}]
                answer = normalize(self.case, events, "No citation", [])
                self.assertEqual(answer.calls[0].tool, kind)

    def test_foreign_mcp_tool_is_not_discarded(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "foreign",
                    "tool": "vault_contribute",
                    "arguments": {},
                },
            }
        ]
        answer = normalize(self.case, events, "No citation", [])
        self.assertEqual(answer.calls[0].tool, "foreign/vault_contribute")

    def test_server_recording_is_preserved(self) -> None:
        call = Call(
            tool="vault_search",
            arguments={"query": "batch"},
            result=self.case.search_response,
        )
        answer = normalize(self.case, [], "No citation", [call])
        self.assertEqual(answer.calls, [call])

    def test_client_and_server_recordings_must_agree(self) -> None:
        call = Call(
            tool="vault_search",
            arguments={"query": "batch", "limit": 10},
            result=self.case.search_response,
        )
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "fixture",
                "tool": "vault_search",
                "arguments": {"query": "batch"},
                "status": "completed",
                "error": None,
                "result": {"structured_content": self.case.search_response},
            },
        }
        verify_call_recording([event], [call])
        with self.assertRaisesRegex(ValueError, "disagree"):
            verify_call_recording([event], [])
        event["item"]["server"] = "production"
        with self.assertRaisesRegex(ValueError, "non-fixture"):
            verify_call_recording([event], [call])

    def test_failed_client_call_cannot_be_packaged_as_success(self) -> None:
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "fixture",
                "status": "failed",
                "error": {"message": "approval required"},
            },
        }
        with self.assertRaisesRegex(ValueError, "Unsuccessful"):
            verify_call_recording([event], [])

    def test_retained_client_events_match_transcripts_and_manifest(self) -> None:
        root = (
            Path(__file__).resolve().parents[2]
            / "app/vault/docs/evaluations/grounding-2026-09-17-terra"
        )
        manifest = read_json(root / "manifest.json")
        for variant in ("baseline", "revised"):
            transcript = read_json(root / variant / "transcript.json")
            self.assertEqual(len(transcript["answers"]), 14)
            for answer in transcript["answers"]:
                relative = f"{variant}/{answer['case_id']}.jsonl"
                raw = (root / relative).read_bytes()
                self.assertEqual(
                    hashlib.sha256(raw).hexdigest(),
                    manifest["artifact_sha256"][relative],
                )
                events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
                calls = [Call.model_validate(call) for call in answer["calls"]]
                verify_call_recording(events, calls)
                messages = [
                    event["item"]["text"]
                    for event in events
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"
                ]
                self.assertEqual(messages[-1], answer["final_answer"].rstrip("\n"))

    def test_prompt_withholds_reviewer_rubric(self) -> None:
        self.case.required_facts = ["PRIVATE RUBRIC SENTINEL"]
        self.case.forbidden_assertions = ["HIDDEN FORBIDDEN SENTINEL"]
        prompt = case_prompt(self.case)
        self.assertIn(self.case.question, prompt)
        self.assertNotIn("SENTINEL", prompt)
        self.assertNotIn(self.case.expected_evidence[0].excerpt, prompt)

    def test_built_in_mutation_and_external_tools_are_disabled(self) -> None:
        for feature in (
            "shell_tool",
            "unified_exec",
            "apps",
            "plugins",
            "hooks",
            "multi_agent",
            "browser_use",
        ):
            self.assertIn(feature, DISABLED_FEATURES)
        self.assertNotIn("code_mode_host", DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
