# 23. Candidacy is a field, and the export projects it into a folder

Date: 2026-08-19. Substantially revised 2026-08-22.

## Status

Accepted 2026-08-22.

The governance changes this required are applied (knowledge-platform `d40bdfc`): the folder is
`canonical: true`, `engine_managed: true`, `ai_write: engine_only`, `validation_mode: agent`,
`allowed_types: ["Agent Note", "Wiki Page"]`; both types carry the folder in `folder_globs`;
and the Promotion Policy describes a projection rather than a drop box. The live vault
validates clean and the engine's schema tests pass against it.

Implementation is outstanding: the `promotion_status` column, the export routing, and the
prune-guard fix in "Consequences" below.

Refines ADR 0022 (two trees, one writer each), which said the exporter "projects `Agent/`
only" without saying which parts of `Agent/`. Depends on ADR 0010 (`vault_path` is the only
policy key) and ADR 0014 (`ai_read` is a rule about agents, not about humans).

**Requires governance changes** to `folders.yml`, `types.yml`, and `Promotion Policy.md` in
the private knowledge-platform repository. Those documents outrank this one; the point of the
revision below is that they were written for the Stage-A vault and describe a mechanism the
service-backed vault should replace, so the right move is to change them deliberately rather
than to obey them by accident.

## Context

ADR 0022 established that `Agent/` is authoritative in the database and reaches a human by
export. Read literally, "projects `Agent/` only" means every row whose `vault_path` begins
`Agent/`. `folders.yml` classifies four folders under that root, and the first draft of this
ADR treated one of them as off limits:

| Folder | `engine_managed` | `ai_write` |
| ------ | ---------------- | ---------- |
| `Agent/notes/` | true | `engine_only` |
| `Agent/review/` | true | `engine_only` |
| `Agent/wiki/` | true | `engine_only` |
| `Agent/Promotion Candidates/` | **false** | `allowed` |

That draft reasoned: the folder is declared human-curated and outside the engine's dedup
gate, so the exporter must not write it, and a promotion queue must be surfaced some other
way.

**That was the wrong conclusion from the right observation.** The declarations are accurate
descriptions of the Stage-A vault, where files *are* the store and a folder is the only place
state can live. In the service-backed vault a file's location under `Agent/` is derived from a
database row, which changes what a folder can mean — and makes the old arrangement unworkable
rather than merely inconvenient.

### Why "move the file" cannot work

The obvious human workflow — drag a note into `Agent/Promotion Candidates/` — has no effect.
The row still says `Agent/notes/<slug>.md`, so the next export rewrites the original and the
moved copy becomes an orphan of nothing. One row cannot be in two places, and the file is not
the row.

So candidacy has to be **data**. Once it is data, the export can put the file wherever the
data says, and the folder becomes a faithful view rather than a second source of truth.

## Decision

**Promotion candidacy is a field on the document. The export routes on it. The folder is a
projection.**

### `promotion_status`

A nullable enum on `vault_documents`, in the shape `vault_review_state` already uses:

| Value | Meaning | Exports to |
| ----- | ------- | ---------- |
| *null* | never proposed | `Agent/notes/` |
| `candidate` | proposed for promotion, awaiting human judgement | `Agent/Promotion Candidates/` |
| `promoted` | a Human note has been written from it | `Agent/notes/` |
| `retracted` | considered and declined | `Agent/notes/` |

Routing is binary — candidate or not — while the field is three-valued so the *outcome* is
recorded. That is the same shape as a review case, where `accepted` and `rejected` both mean
"settled" and are worth telling apart. It is what stops a note being re-proposed forever, and
what lets a reviewer see at a glance that something was already considered and declined.

`promotion_status` is distinct from `status` (the vault's visibility gate) and from
`doc_status` (the Status Map value), for ADR 0011's reason: three different questions, three
different fields, none derived from the others.

### The note stays canonical, and stays in the corpus

`Agent/Promotion Candidates/` becomes `canonical: true`. The first draft inherited
`canonical: false` from the Stage-A declaration, and that is wrong here: non-canonical reads
as *archived* — retired but retained — while a promotion candidate is the opposite, a note
singled out for being **more** valuable than most. Candidacy is elevation, not retirement.

It follows that a candidate stays a first-class agent note: served to agents, returned by
search, and inside the dedup gate. This contradicts the Promotion Policy's line that
candidates are "kept outside the engine's dedup gate on purpose", and does so deliberately.
That sentence protects a Stage-A property — a hand-edited staging file should not be scored
against by a title matcher — and the cost in the service vault is unacceptable: a note would
lose its value to agents precisely because someone judged it valuable to humans.

### Promotion writes a new note; it never moves this one

The Promotion Policy is right about the important part and stays unchanged there: a promoted
note is "a first-class Human note under the Metadata Standard, **not a copied agent note**".
The human rewrites; the agent note is not consumed. That is what makes `promoted` a state the
original can be *in*, rather than a tombstone.

The promoted Human note should carry **`SourceIDs` naming the agent notes it came from**, the
same provenance a compiled `Wiki Page` carries for its sources. It gives traceability, shows
what has already been promoted, and is the natural place a future tool would look to avoid
re-promoting the same material.

### `Agent Note` and `Wiki Page` are both allowed there

A compiled wiki page distilling several notes is, if anything, *more* likely to be
human-worthy than a raw note — synthesis is most of what promotion asks for. So
`allowed_types` becomes `["Agent Note", "Wiki Page"]`, both types gain the folder in their
`folder_globs`, and `validation_mode` becomes `agent` so those types are actually checked
rather than skipped under `loose`.

### What is still true from the first draft

`EXPORTED_PATH_PREFIXES` remains a constant rather than configuration, for the reason
`read_policy.py` gives about `READABLE_PATH_PREFIXES`: which folders a machine may write is a
governance decision, and a deployment must not opt into projecting somewhere the governance
layer did not sanction. The set simply grows by one, and it grows by amending `folders.yml`
first.

Nothing here touches `Human/`. Agents may not write there at all, and `check-policy` enforces
it on `ai/` branches.

## Consequences

### The prune guard has to change, and this is the concrete bug

The exporter refuses to prune a prefix the corpus does not populate, so that an export cannot
delete the Stage-A wiki pages while the service holds no wiki documents. Under this decision
that misfires: when the *last* candidate is promoted or retracted,
`Agent/Promotion Candidates/` has zero rows, the guard skips it, and the final file is
stranded — showing a candidate that is no longer one.

Occupancy is the wrong signal for ownership. The guard must take an explicit set of prefixes
the corpus owns, and sweep an owned prefix even when it is empty, while still refusing one it
does not own. That change belongs with this decision rather than after it.

### Governance edits this requires

Three files, as one reviewed patch:

- `folders.yml` — `Agent/Promotion Candidates/**` becomes `canonical: true`,
  `engine_managed: true`, `ai_write: engine_only`, `validation_mode: agent`,
  `allowed_types: ["Agent Note", "Wiki Page"]`.
- `types.yml` — `Agent Note` and `Wiki Page` gain `Agent/Promotion Candidates/**` in
  `folder_globs`.
- `Promotion Policy.md` — the flow stops describing a folder someone drops files into, and
  the "outside the engine's dedup gate" line is replaced by the reasoning above.

### `vault_path` moves when candidacy changes

Flagging a note rewrites it at a new path and prunes the old file. Content is identical, so
git renders it as a rename and history follows. Acceptable, and the alternative — a stable
path with the folder as an index or a symlink farm — buys nothing a reader would notice.

### The privilege argument is unchanged

ADR 0022's amendment allows contributor input to influence a note's *leaf name* and never its
folder. `promotion_status` is a closed enum set by a reviewer, not free text from a
contributor, and it selects between two service-chosen folders. No caller gains a way to
choose where its note lands.

### Who sets it

`promotion_status` is gated on **`vault:review`**, and its tool belongs on the **admin MCP
surface**, not the general mount — the same answer the review flow got, for the same reason.
Proposing a note for promotion and adjudicating a flagged one are both judgements about what
the corpus should contain, made by a person, and neither belongs on a surface that untrusted
note text can name (ADR 0021).

That makes `vault:review` the scope for two verbs rather than one, which is the right
granularity: a credential that may triage the review queue is the same credential that may
triage the promotion queue, and an ordinary contributor holds neither.

### What this does not decide

The shape of the admin MCP server itself, which remains unstarted and its own decision.
