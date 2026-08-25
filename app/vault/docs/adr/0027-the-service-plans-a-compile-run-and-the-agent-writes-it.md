# 27. The service plans a compile run; the agent writes it

Date: 2026-08-22

## Status

Accepted 2026-08-22, implemented the same day.

Completes the compile half of ADR 0022 (two trees, one writer each) and retires the last
markdown writer outside the service. Depends on ADR 0013 (embedding text), ADR 0016 (the
governed write path), and ADR 0025 (edges are stored, not traversed). Amends ADR 0016's "no
dedup, no write" for one document kind, with reasons.

## Context

`Agent/wiki/` held fourteen pages produced by the Stage-A librarian loop in the
knowledge-platform engine, because the service had no compile path. Everything else about that
tree had already moved: notes are contributed through the service, the export projects them,
and reconciliation keys on `vault_path`. Compilation was the one writer left outside.

The schema has been ready since the first migration. `vault_compile_runs` exists with its
completion CHECK, `document_kind_enum` reserves `wiki`, and
`vault_documents_compile_provenance_consistent` requires `compile_run_id`, `compiled_by` and
`compiled_at` to be NOT NULL for a wiki row — so a page cannot exist without a run, which is
the property this decision has to preserve rather than invent.

## Decision

**The service decides *which* pages need writing. A model writes the prose. The service stores
the result and owns the run.**

Three endpoints, `vault:compile`:

```
POST /compile/runs                  open a run, return work items
POST /compile/runs/{id}/pages       store one page, embedded, attributed to the run
POST /compile/runs/{id}/declines    record notes this run refuses  (added 2026-08-24)
POST /compile/runs/{id}/finish      settle it and publish a frontier
POST /compile/runs/{id}/fail        settle it and publish none
```

*(`finish` still publishes a frontier, and it is history rather than an input — planning reads
declines. See the 2026-08-24 amendment.)*

The division is the Stage-A engine's, and it is the only one available: deciding that a page
is stale is a query, and distilling four notes into a paragraph is not.

### A plan carries note ids, never note bodies

The compiling agent fetches what it needs through the ordinary read surface, which is already
policy-checked (ADR 0014), already paginated, and already the thing every other reader uses.
Inlining bodies would create a second read path with its own disclosure rules — and would make
one response the size of the corpus.

### Three reasons a page is planned, and they are not interchangeable

Ported from the engine's `compute_stale` and kept diffable against it, the way `governance.py`
is kept diffable against `vault_contrib.core` (ADR 0004):

- **`missing`** — the page cites a note that no longer exists. It makes a provenance claim
  that is false.
- **`stale`** — a source moved after the page was compiled, or has since been flagged. The
  synthesis is out of date rather than wrong.
- **`new-source`** — a note no page covers. Not a stale page; an absent one.

A **flagged note is never offered as a new source.** Compiling content the write path declined
to endorse would launder it into a tree the read surface serves freely.

### The frontier is what stops the plan becoming a permanent backlog

A successful run records `output_frontier.frontier_at` — the maximum `updated_at` across
notes. The next incremental plan offers only uncovered notes newer than that. Without it,
every note a librarian consciously chose not to compile would be re-offered forever and the
plan would never be empty, which is the state in which nobody reads it.

Two details that are easy to get backwards, and the first of them **was** — this ADR and the
code both asserted the opposite until 2026-08-23:

- **A successful run publishes the frontier it was *planned* against, not one read at
  `finish`.** Reading it at settlement loses notes permanently: plan at frontier F, a note
  lands at T > F so the plan never mentions it and the compiler never sees it, the run
  finishes and publishes T, and every later incremental plan then skips uncovered notes at or
  below T. That note can never be compiled. Publishing F means the next plan reconsiders
  everything written since planning began — including work this run may already have covered,
  which is the harmless direction, because a page that covers a note removes it from
  `new-source` anyway.
- **A failed run publishes no frontier**, and `_last_frontier` considers only succeeded runs.
  A failed run's frontier would claim coverage for pages it never wrote, so the next plan
  would skip exactly the notes the failure left uncompiled.

### A failed run keeps its pages

Deliberately not a rollback. Pages a run committed are real synthesis and their provenance is
accurate; discarding them to reach a tidier state throws work away. What failure changes is
coverage, not content.

### Compilation skips the dedup gate, and `find_similar` skips wiki pages

**This is the amendment to ADR 0016 and the part most worth arguing with.**

A compiled page restates its sources by construction — that is what compiling *is*. Running
the contribution's dedup gate over it would flag every page ever written, against the very
notes it cites.

The inverse matters more, and it was a latent bug rather than a design question:
`find_similar` did not filter by `kind`, so the moment compilation wrote its first page, that
page joined the corpus notes are scored against. A note whose successor covers the same ground
as a page derived from it would look like a duplicate of a document that exists *only because
that note does*. The two directions are also asymmetric in a way one corpus cannot express: a
page legitimately restates its sources, while a note restating another note is exactly what
the gate exists to catch.

So `find_similar` is notes-only. Today `flag_at` is 1.0 and only an identical embedding flags,
so the practical effect is small — and it will not stay small. The calibration register in
`docs/embedding-calibration.md` exists to lower that threshold as observations accumulate, and
this filter is what stops compilation poisoning the distribution the threshold is derived
from.

**Pages are still embedded.** Search returning synthesis is the point of compiling at all;
what they skip is adjudication, not indexing. A missing embedding provider is therefore a 503
here as it is on the write path — a page nobody can find is not a page.

### `source_ids` are validated; `related_ids` still are not

ADR 0025 keeps edges opaque because a contribution may legitimately reference a note that is
archived, flagged, or not yet written. A wiki page's `source_ids` are different in kind: they
are its **provenance**, the record of what it was synthesized from. Provenance naming
something that never existed is a false claim rather than a dangling edge, so an unresolved
source is a 422. The Stage-A compiler refuses the same way.

### Settling takes the corpus lock

`write_page` re-checks under the corpus advisory lock that its run is still `running`, and
that check is only a guard if the settle path contends for the same lock. `finish` and `fail`
therefore take it too. Without that, `finish` can commit in the window between the check and
the insert, and a page lands attributed to a run that has already reported its result — with
the settlement response and the published frontier both describing a corpus that gained
another page immediately afterwards.

The same lock is why `write_page` re-validates `source_ids` under it rather than trusting the
check it made before embedding. Retirement takes this lock, and an embedding call is seconds
long, so a source can vanish inside that window; validating only up front would store
provenance naming a note that no longer exists.

### One timestamp per run

`compiled_at` comes from the run's `started_at`, not from `now()` at each page. A per-page
clock would make "was this note newer than the page" depend on where in the run the page
happened to be written, which turns staleness into a function of ordering.

This is also why `set_compile_provenance` exists separately from `replace_content`. That
method deliberately leaves compile provenance alone so an ordinary update cannot claim to have
produced a page — but a recompile genuinely is a compilation, and without moving `compiled_at`
a rewritten page would keep the previous run's timestamp and stay permanently stale.

## Consequences

### `Agent/wiki/` can finally be swept, but not yet

`CORPUS_OWNED_PATH_PREFIXES` (ADR 0023) still excludes `Agent/wiki/`, and it must until the
fourteen Stage-A pages exist as rows *in the database being exported*. Until then an
`--apply --prune` export would delete every one of them, because no row accounts for any.

The order is: compilation ships, the existing pages are imported through
`scripts/import_vault_wiki.py`, *then* `Agent/wiki/` joins the owned set. Doing it in the other
order is a data-loss bug with a one-line diff, which is why the constant carries a comment
saying so. `Agent/wiki/_index.md` is generated by the exporter rather than stored, so it is
written into the expected set rather than left as an orphan the sweep would remove.

### The Stage-A librarian loop can be retired

`compile plan` / `compile write` / `compile finish` in the knowledge-platform engine, and the
`knowledge-vault` skill's compile loop, become the second writer to a tree the service now
owns. ADR 0022 gives each tree one writer; this is the point at which that becomes true of
`Agent/wiki/`.

### Runs accumulate

One row per plan, forever, and `ON DELETE RESTRICT` means a run cannot be deleted while any
page cites it. That is correct — provenance must not vanish — and it means pruning is bounded
to runs that produced nothing. A `running` run that is never settled is the case to watch:
`compile_plan` carries the tightest of the three quotas for exactly that reason.

### What this does not decide

Whether wiki pages appear on the MCP tool surface. Compilation is REST-only here, and a
`vault_compile_*` tool set is a separate decision about which agent does the librarian's job.
ADR 0026 settled the general question — privileged tools go on the existing mount, gated by
scope — so if compile tools ever land, that is where and `vault:compile` is the gate.

Whether an `MoC` (map of content) is a distinct type. `types.yml` admits `Wiki Page` and `MoC`
under `Agent/wiki/`; the service writes the former and nothing writes the latter.

## Amendment, 2026-08-23 — a run belongs to the principal that opened it, and the plan is advice

Two things this decision left implicit, one of them wrong.

**Wrong:** `compiler_principal_id` was recorded on the run and never checked. Any holder of
`vault:compile` could write pages into another principal's run and settle it, producing a run
that names one compiler while its pages and its settlement audit events name another —
provenance contradicting itself, which is the one thing a compile run exists to provide.
Settling is worse than writing, because settling publishes a frontier on the opener's behalf and
the frontier is what stops notes being re-offered. All three of `write_page`, `finish` and `fail`
now require the caller to be the opener, refusing with `409`: the scope already permits writing
wiki pages and a holder may open its own run whenever it likes, so this is a consistency rule
rather than a permission the caller lacks.

**Implicit, and deliberate:** the plan is *advice about what is stale*, not an authorization
list. `write_page` does not check that a page, its sources, or its `page_id` appeared in the
run's work items, and `finish` does not check that any planned item was completed. Nothing is
persisted per work item — the run row holds the frontier and the opener, and the items live only
in the `plan()` response.

That follows from what the scope means. `vault:compile` is the permission to write wiki pages;
it is operator-granted, cannot be requested through OAuth (the baseline is read, write, and
non-mutating amendment proposal only),
and is currently granted to nothing. A holder writing a page the plan did not mention is doing
the thing the scope is for. And an empty successful run is not a failure to complete work — it is
the documented way to decline, which is exactly what the frontier exists to record. Re-offering a
note the librarian consciously passed over, every run forever, is the state this ADR was written
to avoid. `all_pages=true` re-offers everything, which is the recovery path when a decline was
wrong.

What that costs is real and worth naming: a compiler that opens a run, writes nothing, and
finishes successfully advances the frontier past notes it never covered, and only
`all_pages=true` will surface them again. Making the plan *binding* — persisting work items and
their state, requiring each write to consume one, refusing `finish` while items are unresolved —
would close that. It was deferred here, and then **superseded**: the amendment below fixes the
same problem at its source for a fraction of the cost, so binding work items are no longer
proposed.

## Amendment, 2026-08-24 — a decline is recorded on the note; the frontier stops planning

**The frontier is replaced by `vault_documents.compile_declined_at`** (migration 0015). The run
row keeps `input_frontier` and `output_frontier` as its own history — when the source corpus
stood where it did — and planning no longer reads either.

### Why: the frontier conflated two states, and the second one really happened

A frontier records *when*. The question the planner is actually asking is *whether somebody
judged this note*, and a timestamp cannot distinguish "considered and refused" from "never
offered at all". That is not theoretical. It is reachable through the ordinary review workflow,
with no misbehaviour anywhere, because two correct decisions meet:

- **A flagged note is never offered as a new source** — decided above, so that compilation
  cannot launder content the write path declined to endorse.
- **`set_status` does not move `updated_at`** — decided in the repository, because adjudicating
  a note is not editing it and the export would otherwise churn every reviewed file.

So: a note is contributed and flagged; a run is planned and the note is excluded from it, but
still counts toward `note_frontier`, which is `max(updated_at)` across every note *whatever its
status*; the run succeeds and publishes that frontier; a reviewer approves the note; its
`updated_at` never moved. It is now active, uncovered, and permanently below the frontier. No
incremental plan will ever offer it again, and only `all_pages=true` recovers it.

Marking the decline cannot express that, because only a note somebody actually declined is
marked. A note nobody was shown stays unmarked and keeps being offered — which is what
`test_a_flagged_note_approved_later_is_still_offered` pins.

### What it looks like

```
POST /compile/runs/{id}/declines     {"note_ids": [...]}
```

- **A decline expires when the note changes.** Stale once `updated_at > compile_declined_at`: a
  note edited since the judgement is a different note. The frontier gave this for free, by
  construction; an explicit decline has to state it, and stating it is better than inheriting it.
- **A separate call, not a field on `finish`.** For the reason pages are separate — a run that
  declines and then fails keeps its declines, exactly as it keeps its pages. Both are real
  judgements and discarding them to reach a tidier state throws the work away.
- **Declining a note the plan did not offer is allowed**, because the plan is advice. Refusing it
  would leave a librarian who noticed something in passing with nowhere to put that.
- **An id that resolves to no live note is a 422**, not a silent no-op, for the reason
  `source_ids` are validated: a decline naming nothing would leave the note offered forever with
  nothing to explain why. A wiki page id resolves to nothing here too — declining is refusing to
  write a page *from a note*, which the column's CHECK also says.
- **`all_pages=true` ignores declines**, which is what makes it the recovery path when a
  judgement was wrong.
- **One audit event per note**, carrying the principal. The note keeps only the timestamp: who
  and when is the audit trail's job (ADR 0002), and a run is a time window, so a range revert
  works without putting a run reference on the note. Migration 0015 records why that reference
  would be actively harmful — it would pin runs open against `ON DELETE RESTRICT`, and
  `vault_documents_compile_provenance_consistent` already says a note carries no compile run.

### The first plan after this deploys is a large one

No note is declined yet, so nothing suppresses anything: the first plan offers every uncovered
active note rather than only those newer than the last run's frontier. That is the intended
effect rather than a migration artefact — it surfaces precisely what the frontier had been
hiding, including whatever the flagged-then-approved case had stranded for good. A librarian
works through it once and declines what it does not want, and from then on the plan is short
because refusals are recorded instead of inferred.

`all_pages=true` becomes almost redundant on that first run, which is the clearest sign the
frontier had been doing two jobs and only admitting to one.

### What this does not change

The plan is still advice. Nothing requires a write to correspond to a work item, and `finish`
still does not check that anything was done — a compiler that declines everything is stating a
position, which is the whole point. What changed is that the position now has to be *stated*
rather than inferred from the clock.
