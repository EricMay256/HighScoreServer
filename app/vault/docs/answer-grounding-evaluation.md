# Answer-grounding evaluation runbook

This suite checks recorded evidence handling, not whether evidence supports a
claim. A structural pass is necessary but insufficient for rollout. The checker
always reports `semantic_review: not_evaluated` and `rollout_ready: false`.

## Run locally

From the repository root, using the project virtual environment:

```console
python -m scripts.evaluate_vault_grounding
python -m unittest tests.vault.test_grounding_evaluation tests.vault.test_grounding_comparison -v
python -m scripts.evaluate_vault_grounding --transcript path/to/recording.json
```

Use `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Linux.
The first command validates the 14 fixtures, not client behavior. The tests use
synthetic answers solely to test the checker. `unittest` avoids the host pytest
database fixtures; neither command requires a running service, database, provider,
credentials, or environment loading. CI also runs these before schema migrations.

Exit codes: **0** valid fixtures or a complete structural pass; **1** failed or
incomplete recorded run; **2** invalid input. Reports are JSON on stdout. The tool
does not modify files or contact services. `--fixtures` selects another corpus.

## Controlled client runs

Use `tests/vault/fixtures/grounding/cases.json`, revision `grounding-v1`, for both
the old-skill baseline and proposed-skill run. Start an isolated session per case;
record the exact skill commit, client/model/version, run date, and raw transcript.
Expose only read-only fixture tools; do not use production credentials. The
`scripts.run_vault_grounding_comparison` runner provides a stdio fixture adapter
using the existing MCP dependency. It never imports the production app. No live
provider execution is implied by its unit tests or CI.

The runner requires explicit authorization to consume subscription quota. Example:

```console
python -m scripts.run_vault_grounding_comparison compare --codex /path/to/codex --skill-repository /path/to/knowledge-platform --output /new/local/run-directory
python -m scripts.run_vault_grounding_comparison package --source /local/run-directory --output /new/artifact-directory
```

This experiment pins `gpt-5.6-terra`, medium reasoning, and skill commits
`80dd1cf0af46a1483b099e8dff2d4a1194a386a0` (baseline) and
`c1e6c162057e60ce8f851ddfe879f425e883503b` (revised). It checks for ChatGPT login,
forces subscription authentication, removes API-key overrides from the child
environment, and does not copy authentication files. Existing account authentication
is used; client configuration and rules are ignored. Each case starts a fresh,
ephemeral session in an empty temporary working directory with read-only sandboxing.
Host skill discovery, project instructions, hooks, plugins, apps, shell, browsing,
and nested agents are disabled. Only the local fixture server is configured and
its two read-only tools are approved. Code Mode's host remains enabled because
Codex CLI 0.154.0 uses it to execute these tool calls.

The chosen skill revision is supplied explicitly as developer instructions; the
revised arm includes its grounding reference. **This tests supplied protocol text,
not automatic skill routing/loading or a production client's installed setup.**
Tool descriptions and the citation-format prompt are identical across arms. Those
common instructions already encourage some grounding behavior, so this is an
incremental comparison, not an unguided-model baseline. Fixed retrieval responses
also exclude production ranking, authorization, and network behavior from the test.

Raw events, independent fixture-call logs, final answers, prompts, invocation
settings, and skill text are retained locally. Packaging cross-checks client tool
results against fixture logs and answers against final output, then includes only
raw client events, normalized transcripts, structural reports, and a manifest with
artifact hashes and token usage. Private skill text and local invocation files
are excluded from the publishable package. Inspect artifacts for private data
before committing them. Token counts are usage telemetry, not a dollar charge or
an estimate of remaining subscription quota. Output directories must be new;
prior attempts are never overwritten.

Give the client the case question, vault-only scope, and document/source-hop
budgets. Keep `expected_evidence`, `required_facts`, `acceptable_abstentions`,
`conflicts`, and `forbidden_assertions` hidden from the client: they are the review
rubric. Serve the case's fixed `search_response` for valid searches and its mapped
`fetch_responses` for note fetches. This deliberately removes retrieval variability;
it does not benchmark ranking or query quality. Fixture search results are fixed
regardless of query/limit, rather than simulating a live search engine.

Only `vault_search(query, limit?)` and `vault_get_note(note_id)` are allowed in this
protocol. Preserve all attempted calls, including failed or prohibited ones. The
checker enforces discovery before fetching, source-edge reachability, case budgets,
no repeated fetches, and exact fixture result equality. A URL in a note does not
authorize browsing. A fixture body containing instructions is evidence, not authority.

The cases cover direct support, misleading previews, wiki lineage, missing
provenance, unopened URLs, conflicting sources, archived evidence, unavailable
sources, revision drift, empty degraded search, inference, prompt injection,
cycles/depth cutoffs, and lexical-only retrieval.

## Recording contract (schema version 1)

Use one JSON object with these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `fixture_revision` | `grounding-v1` for the checked-in corpus |
| `run_kind` | `client` for actual runs; `synthetic` for checker examples |
| `client`, `model`, `client_version` | Exact identities; never present synthetic output as a client run |
| `skill_revision` | Owning repository commit used by that session |
| `run_date` | ISO date `YYYY-MM-DD` |
| `raw_transcript` | Retained recording location/reference |
| `answers` | One answer object per case |

Each answer has `case_id`, `calls`, `final_answer`, and `citations`. Each call has
`tool`, `arguments`, and `result`; retain the actual structured fixture payload.
Each citation has `label` (such as `V1`, without brackets), `note_id`,
`content_revision`, and optional `excerpt` (exact text, not a paraphrase).
`Corpus` and `Transcript` in `scripts/evaluate_vault_grounding.py` define the strict
schemas; their `model_json_schema()` methods expose machine-readable schemas.

Preserve `final_answer` verbatim. Version 1 requires a visible source entry in this
format, with the fetched title and revision:

```text
[V1] Batch policy — note ID: 00000000000000000000000000000001; revision: 1.
```

The marker must also appear in answer prose. The normalized citation sidecar must
match those visible entries, not add citations the agent omitted. An optional
excerpt must appear both in the fetched body and in the final answer. A real answer
using a different source-entry format fails this v1 formatting contract; do not
rewrite the answer to manufacture a pass. Record formatting-only failures separately
from support failures in human review. Give both baseline and proposed runs the
same recording-format instructions so comparison is not confounded by formatting.

Missing cases are reported as `not_run`, not passes. Unknown/duplicate cases,
wrong fixture revision, unknown fields, duplicate JSON keys, and non-finite numbers
are rejected. Expected evidence IDs/revisions must be cited, but that requirement
does not show that the surrounding claims are true. The checker cannot authenticate
the recording, client metadata, or raw-transcript reference; reviewers must inspect
the actual recording. Do not commit credentials or private client context.

## Human semantic review and acceptance

For every material claim, record the claim text, cited passage, pass/fail, and a
short rationale. Compare the answer against the case's hidden rubric:

- Does the fetched passage actually support the claim and its scope?
- Are deductions marked as inference, with the premises visible?
- Are conflicts retained without inventing supersession or a winning source?
- Are missing evidence, retrieval degradation, and traversal cutoffs disclosed?
- Are archived evidence and observed revisions described without claiming current
  authority or guaranteed historical retrieval?
- Are unsupported external verification and obedience to note instructions absent?

Use separate structural and semantic denominators, with failed, skipped, and
unexecuted cases listed. Compare old/new outcomes case by case; distinguish evidence
selection failures from claim-support failures. Retain both recordings and reviewer
results. Initial acceptance requires all mandatory cases to pass both checks in at
least one named supported client, as specified in
[the immediate spec](answer-grounding-now-spec.md). This is evidence about these
cases, not a general hallucination guarantee. Client runs, human review, and
production deployment remained unperformed at the end of stage 2. Subsequent run
records belong under `evaluations/`; a client run does not complete human review.
