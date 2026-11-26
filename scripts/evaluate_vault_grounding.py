"""Offline structural checks for recorded vault answers; never grades entailment.

Run `python -m scripts.evaluate_vault_grounding --help`. Uses existing Pydantic
but imports no application, database, provider, or environment-loading modules.
"""

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


DEFAULT_FIXTURES = (
    Path(__file__).resolve().parents[1] / "tests/vault/fixtures/grounding/cases.json"
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Record):
    note_id: str = Field(min_length=1)
    content_revision: int = Field(ge=1)
    excerpt: str = Field(min_length=1)


class Case(Record):
    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    scope: Literal["vault-only"] = "vault-only"
    search_response: dict[str, Any]
    fetch_responses: dict[str, dict[str, Any]]
    expected_evidence: list[Evidence]
    required_facts: list[str]
    acceptable_abstentions: list[str]
    conflicts: list[str]
    forbidden_assertions: list[str]
    fetch_budget: int = Field(ge=1, le=20)
    source_hop_budget: int = Field(ge=0, le=5)

    @model_validator(mode="after")
    def validate_evidence(self) -> "Case":
        for note_id, response in self.fetch_responses.items():
            if response == {"error": "Note not found"}:
                continue
            if response.get("note_id") != note_id:
                raise ValueError("fetch key must match the returned note ID")
            if not isinstance(response.get("body"), str):
                raise ValueError("fetched evidence requires a body")
            if type(response.get("content_revision")) is not int:
                raise ValueError("fetched evidence requires an integer revision")
            if response["content_revision"] < 1 or not isinstance(
                response.get("title"), str
            ):
                raise ValueError(
                    "fetched evidence requires a positive revision and title"
                )
            sources = response.get("source_ids", [])
            if not isinstance(sources, list) or any(
                not isinstance(s, str) for s in sources
            ):
                raise ValueError("source IDs must be a list of strings")
        for evidence in self.expected_evidence:
            response = self.fetch_responses.get(evidence.note_id, {})
            if response.get(
                "content_revision"
            ) != evidence.content_revision or evidence.excerpt not in response.get(
                "body", ""
            ):
                raise ValueError("expected evidence must resolve to a fixture passage")
        hits = self.search_response.get("hits")
        if not isinstance(hits, list) or any(
            not isinstance(hit, dict) or hit.get("note_id") not in self.fetch_responses
            for hit in hits
        ):
            raise ValueError("search hits must resolve to fixture fetch outcomes")
        if self.search_response.get("vector_status") not in {
            "used",
            "failed",
            "not_configured",
        }:
            raise ValueError("invalid fixture vector_status")
        return self


class Corpus(Record):
    schema_version: Literal[1]
    fixture_revision: str = Field(min_length=1)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> "Corpus":
        date.fromisoformat(self.as_of)
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate fixture case IDs")
        return self


class Call(Record):
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class Citation(Record):
    label: str = Field(pattern=r"^V[1-9][0-9]*$")
    note_id: str
    content_revision: int = Field(ge=1)
    excerpt: str | None = None


class Answer(Record):
    case_id: str
    calls: list[Call]
    final_answer: str = Field(min_length=1)
    citations: list[Citation]


class Transcript(Record):
    schema_version: Literal[1]
    fixture_revision: str
    run_kind: Literal["synthetic", "client"]
    client: str = Field(min_length=1)
    model: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    skill_revision: str = Field(min_length=1)
    run_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    raw_transcript: str = Field(min_length=1)
    answers: list[Answer] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_date(self) -> "Transcript":
        date.fromisoformat(self.run_date)
        return self


def citation_entry(citation: Citation, title: str) -> str:
    """Exact portable source entry required by this version of the normalizer."""
    return (
        f"[{citation.label}] {title} — note ID: {citation.note_id}; "
        f"revision: {citation.content_revision}."
    )


def check_answer(case: Case, answer: Answer) -> list[str]:
    errors: list[str] = []
    fetched: dict[str, dict[str, Any]] = {}
    attempts: set[str] = set()
    depths: dict[str, int] = {}
    searched = False
    for index, call in enumerate(answer.calls):
        prefix = f"call {index + 1}"
        if call.tool == "vault_search":
            query = call.arguments.get("query")
            limit = call.arguments.get("limit", 10)
            if (
                not isinstance(query, str)
                or not query.strip()
                or type(limit) is not int
                or not 1 <= limit <= 50
                or set(call.arguments) - {"query", "limit"}
            ):
                errors.append(f"{prefix}: invalid search arguments")
            if call.result != case.search_response:
                errors.append(f"{prefix}: search result differs from fixture")
                continue
            searched = True
            for hit in case.search_response["hits"]:
                depths[hit["note_id"]] = 0
        elif call.tool == "vault_get_note":
            note_id = call.arguments.get("note_id")
            if not isinstance(note_id, str) or set(call.arguments) != {"note_id"}:
                errors.append(f"{prefix}: invalid fetch arguments")
                continue
            if not searched:
                errors.append(f"{prefix}: fetched before discovery")
            if note_id in attempts:
                errors.append(f"{prefix}: repeated fetch (cycle or redundant read)")
            attempts.add(note_id)
            depth = depths.get(note_id)
            if depth is None or depth > case.source_hop_budget:
                errors.append(
                    f"{prefix}: undiscovered ID or source-hop budget exceeded"
                )
            expected = case.fetch_responses.get(note_id)
            if expected is None or call.result != expected:
                errors.append(f"{prefix}: fetch result differs from fixture")
                continue
            if "error" not in expected:
                fetched[note_id] = expected
                for source_id in expected.get("source_ids", []):
                    new_depth = (depth if depth is not None else 0) + 1
                    depths[source_id] = min(depths.get(source_id, new_depth), new_depth)
        else:
            errors.append(f"{prefix}: prohibited tool {call.tool}")
    if not searched:
        errors.append("no fixture search observed")
    if len(attempts) > case.fetch_budget:
        errors.append("document fetch budget exceeded")

    labels = [citation.label for citation in answer.citations]
    if len(labels) != len(set(labels)):
        errors.append("duplicate citation labels")
    visible_labels = set(re.findall(r"\[(V[1-9][0-9]*)\]", answer.final_answer))
    if visible_labels != set(labels):
        errors.append("visible citation labels and normalized mappings disagree")
    prose = answer.final_answer
    for citation in answer.citations:
        document = fetched.get(citation.note_id)
        if document is None:
            errors.append(f"{citation.label}: citation was never successfully fetched")
            continue
        if document["content_revision"] != citation.content_revision:
            errors.append(f"{citation.label}: citation revision differs from fetch")
        entry = citation_entry(citation, document["title"])
        if entry not in answer.final_answer:
            errors.append(
                f"{citation.label}: source entry absent or differs from mapping"
            )
        prose = prose.replace(entry, "")
        if citation.excerpt is not None and (
            not citation.excerpt.strip()
            or citation.excerpt not in document["body"]
            or citation.excerpt not in answer.final_answer
        ):
            errors.append(
                f"{citation.label}: excerpt missing from body or final answer"
            )
    for label in labels:
        if f"[{label}]" not in prose:
            errors.append(f"{label}: citation is listed but never used in answer prose")
    cited = {(c.note_id, c.content_revision) for c in answer.citations}
    for expected in case.expected_evidence:
        if (expected.note_id, expected.content_revision) not in cited:
            errors.append(f"required evidence not cited: {expected.note_id}")
    return errors


def evaluate(corpus: Corpus, transcript: Transcript) -> dict[str, Any]:
    """Check recorded structure, with absence and semantic review reported explicitly."""
    errors: list[str] = []
    if transcript.fixture_revision != corpus.fixture_revision:
        errors.append("fixture revision mismatch")
    cases = {case.case_id: case for case in corpus.cases}
    results = []
    seen: set[str] = set()
    for answer in transcript.answers:
        if answer.case_id in seen or answer.case_id not in cases:
            errors.append(f"unknown or duplicate answer case: {answer.case_id}")
            continue
        seen.add(answer.case_id)
        failures = check_answer(cases[answer.case_id], answer)
        results.append(
            {"case_id": answer.case_id, "passed": not failures, "errors": failures}
        )
    missing = sorted(set(cases) - seen)
    passed = sum(item["passed"] for item in results)
    return {
        "structural_pass": not errors and not missing and passed == len(cases),
        "passed": passed,
        "failed": len(results) - passed,
        "not_run": missing,
        "total": len(cases),
        "errors": errors,
        "cases": results,
        "run_kind": transcript.run_kind,
        "semantic_review": "not_evaluated",
        "rollout_ready": False,
    }


def read_json(path: Path) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique_keys,
        parse_constant=reject_constant,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--transcript", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = Corpus.model_validate(read_json(args.fixtures))
        if args.transcript is None:
            report = {
                "fixtures_valid": True,
                "cases": len(corpus.cases),
                "client_runs": 0,
            }
        else:
            transcript = Transcript.model_validate(read_json(args.transcript))
            report = evaluate(corpus, transcript)
    except (OSError, ValueError, ValidationError) as exc:
        print(json.dumps({"input_error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return (
        0 if report.get("structural_pass", report.get("fixtures_valid", False)) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
