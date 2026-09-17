"""Run isolated, subscription-authenticated Codex fixture sessions, never production MCP.

The skill text is explicitly injected to test protocol behavior, not skill discovery.
No application imports, provider SDK, API key, or live vault credentials are used.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.evaluate_vault_grounding import (
    DEFAULT_FIXTURES,
    Answer,
    Call,
    Case,
    Citation,
    Corpus,
    Transcript,
    evaluate,
    read_json,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = "engine/.claude/skills/knowledge-vault"
MODEL = "gpt-5.6-terra"
REASONING = "medium"
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "hooks",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "shell_tool",
    "unified_exec",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "view_image",
    "skill_search",
    "skill_mcp_dependency_install",
    "in_app_browser",
    "tool_suggest",
    "goals",
)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def git_text(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository.as_posix()}",
            "-C",
            str(repository),
            *arguments,
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return result.stdout


def skill_text(repository: Path, revision: str, revised: bool) -> str:
    text = git_text(repository, "show", f"{revision}:{SKILL_PATH}/SKILL.md")
    if revised:
        text += "\n\n# Loaded reference: answer-grounding.md\n\n"
        text += git_text(
            repository,
            "show",
            f"{revision}:{SKILL_PATH}/references/answer-grounding.md",
        )
    return text


def case_prompt(case: Case) -> str:
    return (
        f"Answer this vault-only question: {case.question}\n"
        f"Document fetch budget: {case.fetch_budget} distinct documents. "
        f"Source-hop budget: {case.source_hop_budget}.\n"
        "Use only the supplied vault tools. Do not save the answer or change any data.\n"
        "Recording format (same in both comparison variants): use [V1], [V2], etc. "
        "beside claims when citing vault documents. List each cited source on its own "
        "line exactly as '[V1] TITLE — note ID: FULL_ID; revision: INTEGER.' "
        "using that document's title, full ID and revision. Do not wrap the source "
        "entry in backticks or emphasis. Answer in ordinary text/Markdown, not JSON."
    )


def serve(fixtures: Path, case_id: str, record: Path) -> None:
    """Read-only stdio fixture adapter; the reviewer rubric is never returned."""
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    corpus = Corpus.model_validate(read_json(fixtures))
    case = next(case for case in corpus.cases if case.case_id == case_id)
    server = MCPServer("grounding-fixture")

    def record_call(
        tool: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        with record.open("a", encoding="utf-8") as stream:
            stream.write(
                Call(tool=tool, arguments=arguments, result=result).model_dump_json()
                + "\n"
            )
        return result

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def vault_search(query: str, limit: int = 10) -> dict[str, Any]:
        """Search the vault. Returns discovery hits, not full document bodies."""
        return record_call(
            "vault_search", {"query": query, "limit": limit}, case.search_response
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def vault_get_note(note_id: str) -> dict[str, Any]:
        """Fetch a full active or archived document by its complete note ID."""
        return record_call(
            "vault_get_note",
            {"note_id": note_id},
            case.fetch_responses.get(note_id, {"error": "Note not found"}),
        )

    server.run(transport="stdio")


def normalize(
    case: Case, events: list[dict[str, Any]], final: str, calls: list[Call]
) -> Answer:
    """Extract mappings from visible source entries; never repair the answer."""
    citations = [
        Citation(label=match[0], note_id=match[1], content_revision=int(match[2]))
        for match in re.findall(
            r"^\[(V[1-9][0-9]*)\] .+ — note ID: ([^;\s]+); revision: ([1-9][0-9]*)\.$",
            final,
            re.MULTILINE,
        )
    ]
    # Retain non-fixture actions as failures rather than silently dropping them.
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed":
            continue
        kind = item.get("type")
        if kind in {"command_execution", "file_change", "web_search"}:
            calls.append(Call(tool=kind, arguments={}, result=item))
        elif kind == "mcp_tool_call" and (
            item.get("server") != "fixture"
            or item.get("tool") not in {"vault_search", "vault_get_note"}
        ):
            calls.append(
                Call(
                    tool=f"{item.get('server')}/{item.get('tool')}",
                    arguments=item.get("arguments", {}),
                    result=item,
                )
            )
    return Answer(
        case_id=case.case_id, calls=calls, final_answer=final, citations=citations
    )


def verify_call_recording(events: list[dict[str, Any]], calls: list[Call]) -> None:
    """Cross-check the independent adapter log against what the client received."""
    observed: list[Call] = []
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call":
            continue
        if item.get("server") != "fixture":
            raise ValueError("A non-fixture MCP server was used")
        if item.get("status") != "completed" or item.get("error") is not None:
            raise ValueError("Unsuccessful MCP call in client recording")
        arguments = dict(item["arguments"])
        if item["tool"] == "vault_search":
            arguments.setdefault("limit", 10)
        result = item["result"].get("structured_content")
        if not isinstance(result, dict):
            raise ValueError("Expected structured fixture result in client recording")
        observed.append(Call(tool=item["tool"], arguments=arguments, result=result))
    if observed != calls:
        raise ValueError("Client events and independent fixture log disagree")


def package_recording(source: Path, destination: Path, fixtures: Path) -> None:
    """Publish only fixture recordings, never private skill text or client config."""
    corpus = Corpus.model_validate(read_json(fixtures))
    destination.mkdir(parents=True, exist_ok=False)
    manifest = read_json(source / "manifest.json")
    manifest["artifact_sha256"] = {}
    for variant in ("baseline", "revised"):
        transcript = Transcript.model_validate(
            read_json(source / variant / "transcript.json")
        )
        if {answer.case_id for answer in transcript.answers} != {
            case.case_id for case in corpus.cases
        }:
            raise ValueError("Cannot package an incomplete comparison")
        variant_dir = destination / variant
        variant_dir.mkdir()
        manifest.setdefault("skill_text_sha256", {})[variant] = hashlib.sha256(
            (source / variant / "skill.txt").read_bytes()
        ).hexdigest()
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        for answer in transcript.answers:
            case_dir = source / variant / answer.case_id
            events_text = (case_dir / "events.jsonl").read_text(encoding="utf-8")
            events = [json.loads(line) for line in events_text.splitlines()]
            calls = [
                Call.model_validate_json(line)
                for line in (case_dir / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            verify_call_recording(events, calls)
            if answer.calls != calls:
                raise ValueError("Normalized calls differ from independent recording")
            if answer.final_answer != (case_dir / "answer.md").read_text(
                encoding="utf-8"
            ):
                raise ValueError("Normalized answer differs from client output")
            # Preserve raw event lines, including warnings and usage; do not cherry-pick.
            raw_path = variant_dir / f"{answer.case_id}.jsonl"
            raw_path.write_bytes((case_dir / "events.jsonl").read_bytes())
            manifest["artifact_sha256"][f"{variant}/{raw_path.name}"] = hashlib.sha256(
                raw_path.read_bytes()
            ).hexdigest()
            for event in events:
                if event.get("type") == "turn.completed":
                    for key in usage:
                        usage[key] += event["usage"].get(key, 0)
        manifest.setdefault("usage", {})[variant] = usage
        transcript.raw_transcript = f"{variant}/<case_id>.jsonl"
        write_json(variant_dir / "transcript.json", transcript.model_dump())
        write_json(variant_dir / "structural-report.json", evaluate(corpus, transcript))
    write_json(destination / "manifest.json", manifest)


def run_case(
    codex: Path, case: Case, text: str, output: Path, fixtures: Path
) -> Answer:
    output.mkdir(parents=True, exist_ok=False)
    (output / "prompt.txt").write_text(case_prompt(case), encoding="utf-8")
    call_log = output / "calls.jsonl"
    final_path = output / "answer.md"
    with tempfile.TemporaryDirectory(prefix="vault-grounding-") as workspace:
        config: dict[str, Any] = {
            "model_reasoning_effort": REASONING,
            "forced_login_method": "chatgpt",
            "approval_policy": "never",
            "project_doc_max_bytes": 0,
            "web_search": "disabled",
            "features.skip_host_skill_discovery": True,
            "developer_instructions": text,
            "mcp_servers.fixture.command": sys.executable,
            "mcp_servers.fixture.args": [
                "-m",
                "scripts.run_vault_grounding_comparison",
                "serve",
                "--fixtures",
                str(fixtures),
                "--case",
                case.case_id,
                "--record",
                str(call_log),
            ],
            "mcp_servers.fixture.cwd": str(ROOT),
            "mcp_servers.fixture.required": True,
            "mcp_servers.fixture.approval_mode": "approve",
            "mcp_servers.fixture.enabled_tools": ["vault_search", "vault_get_note"],
        }
        command = [
            str(codex),
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            MODEL,
            "--json",
            "--color",
            "never",
            "--cd",
            workspace,
            "--output-last-message",
            str(final_path),
        ]
        for feature in DISABLED_FEATURES:
            command.extend(["--disable", feature])
        for key, value in config.items():
            # JSON scalars/arrays are also valid TOML values here.
            command.extend(["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"])
        command.append("-")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"}
        }
        write_json(
            output / "invocation.json",
            {
                "model": MODEL,
                "reasoning_effort": REASONING,
                "config": {
                    key: value
                    for key, value in config.items()
                    if key != "developer_instructions"
                },
                "disabled_features": DISABLED_FEATURES,
            },
        )
        with (
            (output / "events.jsonl").open("w", encoding="utf-8") as stdout,
            (output / "stderr.txt").open("w", encoding="utf-8") as stderr,
        ):
            result = subprocess.run(
                command,
                input=case_prompt(case),
                encoding="utf-8",
                stdout=stdout,
                stderr=stderr,
                env=environment,
                timeout=240,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Codex exited {result.returncode}; inspect {output / 'stderr.txt'}"
            )
    events = [
        json.loads(line)
        for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not any(event.get("type") == "turn.completed" for event in events):
        raise RuntimeError(f"No completed client turn: {output}")
    for event in events:
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call" and item.get("status") == "failed":
            raise RuntimeError(
                f"Fixture transport/approval failure; invalid harness run: {output}"
            )
        if item.get("type") == "error" and "unavailable" in item.get("message", ""):
            raise RuntimeError(
                f"Client capability unavailable; invalid harness run: {output}"
            )
    final = final_path.read_text(encoding="utf-8")
    calls = (
        [
            Call.model_validate_json(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        if call_log.exists()
        else []
    )
    return normalize(case, events, final, calls)


def compare(args: argparse.Namespace) -> None:
    corpus = Corpus.model_validate(read_json(args.fixtures))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    version = subprocess.run(
        [str(args.codex), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    auth = subprocess.run(
        [str(args.codex), "login", "status"], capture_output=True, text=True, check=True
    )
    if "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
        raise RuntimeError("Subscription login required; API billing is not authorized")
    revisions = {
        "baseline": git_text(args.skill_repository, "rev-parse", "c1e6c16^").strip(),
        "revised": git_text(args.skill_repository, "rev-parse", "c1e6c16").strip(),
    }
    write_json(
        output / "manifest.json",
        {
            "client": version,
            "model": MODEL,
            "reasoning_effort": REASONING,
            "auth": "ChatGPT subscription",
            "skill_revisions": revisions,
            "fixture_revision": corpus.fixture_revision,
            "fixture_sha256": hashlib.sha256(args.fixtures.read_bytes()).hexdigest(),
            "started_at": datetime.now(UTC).isoformat(),
            "skill_loading": "explicit developer instructions; not automatic discovery",
            "human_semantic_review": "pending",
        },
    )
    selected = (
        corpus.cases
        if not args.case
        else [case for case in corpus.cases if case.case_id == args.case]
    )
    if not selected:
        raise ValueError("Unknown case")
    for variant, revision in revisions.items():
        text = skill_text(args.skill_repository, revision, variant == "revised")
        variant_dir = output / variant
        variant_dir.mkdir()
        (variant_dir / "skill.txt").write_text(text, encoding="utf-8")
        answers: list[Answer] = []
        for case in selected:
            print(f"Running {variant}/{case.case_id}", flush=True)
            answer = run_case(
                args.codex,
                case,
                text,
                variant_dir / case.case_id,
                args.fixtures.resolve(),
            )
            answers.append(answer)
            transcript = Transcript(
                schema_version=1,
                fixture_revision=corpus.fixture_revision,
                run_kind="client",
                client="Codex CLI",
                model=MODEL,
                client_version=version,
                skill_revision=revision,
                run_date=datetime.now(UTC).date().isoformat(),
                raw_transcript=f"{variant}/<case>/events.jsonl and calls.jsonl",
                answers=answers,
            )
            write_json(variant_dir / "transcript.json", transcript.model_dump())
            report = evaluate(corpus, transcript)
            write_json(variant_dir / "structural-report.json", report)
            print(
                f"Completed {variant}/{case.case_id}: {report['cases'][-1]}", flush=True
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    server.add_argument("--case", required=True)
    server.add_argument("--record", type=Path, required=True)
    runner = commands.add_parser("compare")
    runner.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    runner.add_argument("--codex", type=Path, required=True)
    runner.add_argument("--skill-repository", type=Path, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument(
        "--case", help="Optional harness smoke case, not a full comparison"
    )
    package = commands.add_parser("package")
    package.add_argument("--source", type=Path, required=True)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.fixtures, args.case, args.record)
    elif args.command == "compare":
        compare(args)
    else:
        package_recording(args.source, args.output, args.fixtures)


if __name__ == "__main__":
    main()
