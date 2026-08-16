# 10. `vault_path` is the only policy key; there is no resolved scope column

Date: 2026-07-29

## Status

Accepted

## Context

`folders.yml` keys write permission on vault path. `vault_documents` carried no path, so the
permission model could not be evaluated against the database at all, and the importer had nothing
to write. This blocked the importer.

The obvious design is a denormalized `policy_scope` column holding the governance rule a document
falls under, so a permission check is an equality test instead of string work. Reading the actual
engine kills it.

**A document's policy context is a fold over several rules, not one winning rule.**
`GovernanceSchema.matching_rules()` returns *every* rule whose glob matches, sorted by
`specificity` — the length of the literal prefix before the first wildcard — least-specific first.
`inheritance.resolve_context()` then overlays them in order. The overlay is not uniform:

- `layer`, `canonical`, `engine_managed`, `ai_write`, `validation_mode` are overwritten
  unconditionally by each successive rule.
- `default_type`, `allowed_types`, `purpose` override **only when the rule sets them**, so a
  less-specific ancestor's value survives when the deeper rule is silent.

`Human/01 Inbox/AI/x.md` assembles its context from three rules — `Human/**`, then
`Human/01 Inbox/**`, then `Human/01 Inbox/AI/**` — ending at `ai_write: allowed`,
`canonical: false`, `validation_mode: loose`, `purpose: "AI Suggestion"`. No single glob names
that result. A scope column would therefore have to store either one rule, which is wrong, or the
whole resolved bundle, which is worse: it would materialize governance state into the store that
governance governs, and deferred decision 2 already says policy cannot live inside the store it
governs or it can rewrite its own rules.

The other half of the picture is what makes the plain path sufficient. **Every glob in
`folders.yml` is a literal prefix followed by `/**`** — all seventeen of them, no interior
wildcards. So "which rules match this path" is "which prefixes does this path start with", and
"most specific" is "longest matching prefix". That is an ordinary index-supported string
operation, not a glob engine.

## Decision

**Store `vault_path TEXT NOT NULL UNIQUE` and nothing else. Resolve policy by running the
governance engine's own overlay against it.**

The path is the vault-root-relative posix path with the extension, byte-identical to the
`rel_path` the scanner produces (`path.relative_to(vault_root).as_posix()`). Anything else would
mean HSS matching rules against a string the governance engine never sees.

- **NOT NULL.** The path is what ties a row to its file. A row without one cannot be projected,
  cannot be permission-checked, and cannot be reconciled against disk — there is no useful thing
  to do with it. Unlike `doc_type`, "unknown" is not a meaningful state here.
- **UNIQUE.** One file, one row. Two rows claiming a path is a state the projector cannot resolve,
  and for the human layer, where the file is the source of truth, the path *is* the natural key.
- **A CHECK for shape only** — non-blank, no leading or trailing slash, no empty segment, no `.`
  or `..` segment, no backslash, 1024 characters. Which folders exist stays `folders.yml`'s
  business, exactly as ADR 0009 split shape from vocabulary for `doc_type`.
- **A `text_pattern_ops` btree index.** The UNIQUE constraint's index uses the database collation
  and cannot serve `LIKE 'prefix%'` unless the database is C-collated; `text_pattern_ops` always
  can, which is what makes longest-prefix resolution an index scan.

Traversal segments are rejected at the database rather than only in application code because the
path is used to address a file on the projector's machine. A `..` segment that reached the table
would be a path-traversal primitive stored as data, and the constraint that stops it should not be
the one that is easiest to forget to call.

## Consequences

Migration `0003_vault_path_doc_status` adds the column. Existing rows are backfilled to
`Agent/notes/<id>.md` before `SET NOT NULL`: the write path is unbuilt and the vault has never
been enabled in production, so every row that can exist is an agent note written by a test fixture
or the demo seeder, and that path is what those rows genuinely are rather than an invented value.
If a later import claims the same path the UNIQUE constraint refuses it, which is the right
outcome.

`NewVaultDocument` now requires a path, so every construction site supplies one — the two test
fixtures, the demo seeder, and any future write path. That is deliberate: it makes "what is this
document's path" a question the caller must answer at insert time rather than one deferred until
the projector needs it.

**HSS must port `resolve_context` rather than reimplement prefix matching.** The overlay's
asymmetry — five fields unconditional, three conditional — is not something to re-derive from the
YAML by eye, and the architecture doc already commits to porting the non-secret validation and
decision-policy behaviour with its tests. Until that port lands, nothing in HSS evaluates
`folders.yml` at all; this ADR only guarantees the column that makes it possible.

`vault_path` is on the read surface. A citation is more useful with the source's location than
without it, and for the human layer the path is how a caller finds the note in Obsidian.

**Two things this does not solve, both belonging to the importer.** A replica of a
Markdown-authored layer goes stale when the file changes, so rows need a content hash or mtime to
detect drift; and a deleted file must lose its row, which one-way import does not do by itself.
Neither is a column this ADR can add sensibly before the import direction per layer is settled.

**A note on `folders.yml`'s own documentation.** Its header says rules are matched
"MOST-SPECIFIC-FIRST (the rule with the longest literal prefix wins)". That describes the effect
for the five unconditional fields but not for `default_type`, `allowed_types`, and `purpose`. No
current rule pair exercises the difference, so comment and code agree on today's data; the code is
simply more general. Worth knowing before adding a nested rule that sets one of those three.
