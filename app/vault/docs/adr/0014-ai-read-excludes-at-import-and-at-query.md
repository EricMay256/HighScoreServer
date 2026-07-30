# 14. `ai_read` excludes at import and again at query time

Date: 2026-07-29

## Status

Accepted

## Context

Once the database holds human-authored notes (ADR 0012), a single shared
`VAULT_READ_API_KEY` serves the entire corpus to anyone holding it — including
`Human/07 People/`, which is notes about real people. The operator wants agents to read
useful parts of the human vault and to exclude others; the alternative, duplicating notes
solely to grant agents access, is worse for every reason.

`folders.yml` had no read policy at all. It carries `ai_write` in four values and nothing
about reading. The prose it mirrors, `AI Contribution Policy.md`, is likewise a *write*
policy — its one adjacent sentence says agents may suggest findings "anywhere", which
presumes unrestricted reading.

Two facts from the governance engine shaped this.

**`ai_write` is enforceable because writing leaves a diff.** `policy.py` runs `git diff`
on AI branches and flags changes to non-writable paths. **Reading leaves no trace**, so
there is no CI gate that can enforce a read policy, and there never will be. `ai_read` is
therefore a declaration a runtime obeys, not a rule CI checks — a genuine change in
character from every other field in that file, and the reason the prose says so explicitly.

**A local agent with a checkout still sees everything.** The vault is a git repository on
disk. This policy governs the *hosted* surface, where the exposure is a bearer token that
may be shared, logged, or leaked. Conflating the two would make the rule look broken to
anyone who noticed an agent reading a People note locally.

The integration spec settles a question that looked open: "HSS may remain a public
repository. Its schema, routes, retrieval logic, and **governance implementation are not
secrets**; the corpus and credentials are." Naming readable folders in HSS source is
therefore sanctioned, and an earlier worry about disclosing vault structure was unfounded.

## Decision

**`ai_read` is declared per folder in `folders.yml`, defaults to `forbidden`, and is
enforced twice: excluded folders are never imported, and the read surface filters again.**

**Fail closed.** A folder that says nothing is unreadable. The operator described the need
as a denylist — exclude People and Meetings — and an allowlist was chosen instead because
under a denylist a folder created next year becomes agent-readable the moment it exists,
silently. `folders.yml` already carries one entry per folder, so the cost is one line each.

**Excluded folders are not imported.** Not holding data beats filtering data: it protects
every query, every log line, and every endpoint that does not exist yet, all at once. This
is available here only because the database is not a faithful-replica obligation for the
human layer — the projector regenerates `Vault/Agent/` and never `Vault/Human/`, whose
Markdown is the source of truth — so omitting rows costs nothing downstream.

**The read surface enforces it anyway.** `app/vault/read_policy.py` states the readable
prefixes and supplies both a Python predicate and a SQL one. Both search arms, the fusion
hydration step, and fetch-by-ID apply it. The layers cover different failures: not
importing cannot protect a row imported while a folder was readable and reclassified
afterwards, and query filtering cannot protect against a path that never goes through a
query. Neither subsumes the other.

`READABLE_PATH_PREFIXES` is a module constant, not configuration, for the reason ADR 0008
gives about `READABLE_STATUSES`: a deployment must not be able to opt into serving content
the governance layer excluded.

`get_by_id` gains `readable_only`, defaulting **off**, exactly as `statuses` did — and for
a sharper reason than review tooling. **Reconciliation must be able to load an excluded row
in order to delete it.** A repository that filtered by default would make the sweep unable
to see the rows it exists to remove.

## Consequences

Reclassifying a folder from readable to excluded deletes its rows on the next
mark-and-sweep rather than merely hiding them, because ADR 0012's "what should exist" set
is filtered by `ai_read`. Until that sweep runs the query filter is what withholds them,
which is precisely the window the second layer exists for.

Excluding People costs less than it appears. Person references live in frontmatter and body
text — `Owner`, `Attendees`, `Collaborators`, `[[Alice]]` — not in `related_ids`, so an
excluded note creates no dangling *row* reference. The referring note still carries the
name; the agent simply cannot open the note behind it. A metadata-only tier was considered
for resolving such references and rejected as unnecessary once this was clear.

**`EXCLUDED_PATH_PREFIXES` is empty and deliberately present.** No `ai_read: forbidden`
rule currently nests under an `allowed` one, so the readable union needs no holes today. A
union cannot express such a hole on its own, and discovering that at the moment someone
adds the rule is discovering it too late. A test asserts every listed exclusion actually
sits inside a readable prefix, so it cannot silently become decoration.

**The prefixes are duplicated between `folders.yml` and HSS**, and this is the real cost.
HSS does not load the governance YAML — that would mean the private repository's schemas in
the public one, on a copy that can go stale either way. Two evaluators of one rule can
drift, so a test asserts the SQL and Python predicates agree across readable, excluded, and
unclassified paths. That guards HSS's internal consistency; it cannot detect divergence from
`folders.yml`. Closing that gap properly means porting `resolve_context` and copying the
schemas with a recorded source hash, which the architecture doc already plans and this ADR
does not pre-empt.

**Defaults matter more than the list.** The listed prefixes will be wrong at some point —
a folder gets renamed, a new one appears. Failing closed means the failure mode is an agent
that cannot read something it should, which is visible and annoying, rather than one that
reads something it should not, which is silent.
