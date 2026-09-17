"""Offline checker tests. Run with unittest to avoid host pytest DB fixtures.

Synthetic answers here test the checker, not agent behavior or semantic accuracy.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from scripts.evaluate_vault_grounding import (
    DEFAULT_FIXTURES,
    Answer,
    Call,
    Case,
    Citation,
    Corpus,
    Transcript,
    check_answer,
    citation_entry,
    evaluate,
    read_json,
)


def synthetic_answer(case: Case) -> Answer:
    calls = [
        Call(
            tool="vault_search",
            arguments={"query": case.question},
            result=case.search_response,
        )
    ]
    queue = [(hit["note_id"], 0) for hit in case.search_response["hits"]]
    seen: set[str] = set()
    while queue and len(seen) < case.fetch_budget:
        note_id, depth = queue.pop(0)
        if note_id in seen or depth > case.source_hop_budget:
            continue
        response = case.fetch_responses.get(note_id)
        if response is None:
            continue
        seen.add(note_id)
        calls.append(
            Call(tool="vault_get_note", arguments={"note_id": note_id}, result=response)
        )
        queue.extend((source, depth + 1) for source in response.get("source_ids", []))
    citations = [
        Citation(
            label=f"V{index}",
            note_id=evidence.note_id,
            content_revision=evidence.content_revision,
        )
        for index, evidence in enumerate(case.expected_evidence, 1)
    ]
    prose = "Synthetic checker example; no semantic verdict. "
    prose += " ".join(f"[{citation.label}]" for citation in citations)
    entries = [
        citation_entry(citation, case.fetch_responses[citation.note_id]["title"])
        for citation in citations
    ]
    return Answer(
        case_id=case.case_id,
        calls=calls,
        final_answer=prose + "\n\n" + "\n".join(entries),
        citations=citations,
    )


class GroundingEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = Corpus.model_validate(read_json(DEFAULT_FIXTURES))
        self.case = self.corpus.cases[0]
        self.answer = synthetic_answer(self.case)

    def transcript(self, answers: list[Answer]) -> Transcript:
        return Transcript(
            schema_version=1,
            fixture_revision=self.corpus.fixture_revision,
            run_kind="synthetic",
            client="unit-test",
            model="none",
            client_version="1",
            skill_revision="not-loaded",
            run_date="2026-09-16",
            raw_transcript="synthetic unit test; not a client recording",
            answers=answers,
        )

    def assert_rejected(self, fragment: str) -> None:
        self.assertTrue(
            any(fragment in error for error in check_answer(self.case, self.answer))
        )

    def test_all_fixture_cases_and_synthetic_answers_are_structurally_valid(
        self,
    ) -> None:
        answers = [synthetic_answer(case) for case in self.corpus.cases]
        report = evaluate(self.corpus, self.transcript(answers))
        self.assertTrue(report["structural_pass"], report)
        self.assertEqual(report["passed"], 14)
        self.assertEqual(report["semantic_review"], "not_evaluated")
        self.assertFalse(report["rollout_ready"])

    def test_citation_from_search_only_is_rejected(self) -> None:
        self.answer.calls = self.answer.calls[:1]
        self.assert_rejected("never successfully fetched")

    def test_forged_tool_body_cannot_make_a_quote_valid(self) -> None:
        self.answer.calls[1].result = {
            **self.answer.calls[1].result,
            "body": "fabricated",
        }
        self.assert_rejected("fetch result differs")

    def test_revision_must_be_the_fetched_version(self) -> None:
        self.answer.citations[0].content_revision = 2
        self.assert_rejected("revision differs")

    def test_excerpt_must_exist_in_body_and_answer(self) -> None:
        self.answer.citations[0].excerpt = "invented quotation"
        self.answer.final_answer += "invented quotation"
        self.assert_rejected("excerpt missing")
        self.answer.citations[0].excerpt = self.case.expected_evidence[0].excerpt
        self.assert_rejected("excerpt missing")
        self.answer.final_answer += self.case.expected_evidence[0].excerpt
        self.assertEqual(check_answer(self.case, self.answer), [])

    def test_sidecar_mapping_cannot_add_an_absent_citation(self) -> None:
        self.answer.final_answer = "There are 25 sources."
        self.assert_rejected("mappings disagree")

    def test_source_list_alone_is_not_claim_attribution(self) -> None:
        self.answer.final_answer = self.answer.final_answer.split("\n\n")[1]
        self.assert_rejected("never used")

    def test_source_entry_must_preserve_actual_title_id_and_revision(self) -> None:
        self.answer.final_answer = self.answer.final_answer.replace(
            "Batch policy", "Other policy"
        )
        self.assert_rejected("source entry absent")

    def test_duplicate_labels_and_unmapped_labels_are_rejected(self) -> None:
        self.answer.citations.append(self.answer.citations[0].model_copy())
        self.assert_rejected("duplicate citation")
        self.answer.final_answer += " [V99]"
        self.assert_rejected("mappings disagree")

    def test_mutation_and_external_tools_are_prohibited_even_if_they_fail(self) -> None:
        for tool in (
            "vault_contribute",
            "vault_propose_note_span_edit",
            "web_search",
            "shell",
        ):
            with self.subTest(tool=tool):
                self.answer = synthetic_answer(self.case)
                self.answer.calls.append(
                    Call(tool=tool, arguments={}, result={"error": "denied"})
                )
                self.assert_rejected("prohibited tool")

    def test_fetch_before_search_is_rejected(self) -> None:
        self.answer.calls.reverse()
        self.assert_rejected("before discovery")

    def test_unavailable_source_is_not_citable(self) -> None:
        self.case = next(
            case for case in self.corpus.cases if case.case_id == "unavailable"
        )
        self.answer = synthetic_answer(self.case)
        self.answer.citations[0].note_id = self.case.fetch_responses[
            next(iter(self.case.fetch_responses))
        ]["source_ids"][0]
        self.assert_rejected("never successfully fetched")

    def test_cycles_and_budgets_are_checked(self) -> None:
        self.answer.calls.append(self.answer.calls[1].model_copy(deep=True))
        self.assert_rejected("repeated fetch")
        self.case = next(
            case for case in self.corpus.cases if case.case_id == "wiki-lineage"
        )
        self.answer = synthetic_answer(self.case)
        self.case.fetch_budget = 1
        self.assert_rejected("document fetch budget")
        self.case.source_hop_budget = 0
        self.assert_rejected("source-hop budget")

    def test_required_evidence_cannot_be_omitted(self) -> None:
        self.answer.citations = []
        self.answer.final_answer = "Unsupported answer without citations."
        self.assert_rejected("required evidence not cited")

    def test_partial_run_reports_not_run_cases(self) -> None:
        report = evaluate(self.corpus, self.transcript([self.answer]))
        self.assertFalse(report["structural_pass"])
        self.assertEqual(len(report["not_run"]), 13)
        self.assertEqual(report["passed"], 1)

    def test_unknown_duplicate_cases_and_wrong_fixture_revision_fail(self) -> None:
        run = self.transcript([self.answer, self.answer])
        run.fixture_revision = "wrong"
        report = evaluate(self.corpus, run)
        self.assertEqual(len(report["errors"]), 2)
        run.answers[1] = self.answer.model_copy(update={"case_id": "unknown"})
        self.assertTrue(evaluate(self.corpus, run)["errors"])

    def test_fixture_rejects_broken_evidence_and_duplicate_cases(self) -> None:
        fixture = self.corpus.model_dump()
        fixture["cases"][0]["expected_evidence"][0]["excerpt"] = "not in source"
        with self.assertRaises(ValidationError):
            Corpus.model_validate(fixture)
        fixture = self.corpus.model_dump()
        fixture["cases"].append(fixture["cases"][0])
        with self.assertRaises(ValidationError):
            Corpus.model_validate(fixture)

    def test_cli_exit_codes_and_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.json"
            path.write_text(
                self.transcript([self.answer]).model_dump_json(), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.evaluate_vault_grounding",
                    "--transcript",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["passed"], 1)
            path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.evaluate_vault_grounding",
                    "--transcript",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(
                "duplicate JSON key", json.loads(result.stdout)["input_error"]
            )

    def test_malformed_fixture_payloads_fail_validation(self) -> None:
        for field, value in (
            ("content_revision", 0),
            ("source_ids", "not-a-list"),
            ("title", None),
        ):
            with self.subTest(field=field):
                fixture = self.corpus.model_dump()
                document = next(iter(fixture["cases"][0]["fetch_responses"].values()))
                document[field] = value
                with self.assertRaises(ValidationError):
                    Corpus.model_validate(fixture)
        fixture = self.corpus.model_dump()
        fixture["cases"][0]["search_response"]["hits"] = [42]
        with self.assertRaises(ValidationError):
            Corpus.model_validate(fixture)

    def test_nonfinite_json_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"value": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite"):
                read_json(path)


if __name__ == "__main__":
    unittest.main()
