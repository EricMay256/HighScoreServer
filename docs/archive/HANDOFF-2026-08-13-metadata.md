> **Archived 2026-09-02.** A historical handoff, kept for its reasoning. It is not
> maintained: every fact below describes the state on the date it was written and
> may since have changed. The current picture is in [`docs/NEXT-STEPS.md`](../NEXT-STEPS.md)
> and [`docs/HANDOFF.md`](../HANDOFF.md).

# Handoff — metadata model and the edge graph

A decision brief, not a task list. Six decisions about tags, facets, and relations that are
entangled: deciding any one of them in isolation forecloses options on the others, and two of
them cannot be decided at all without a measurement nobody has run yet.

**Read `HANDOFF.md` first** for repo/branch/database state and the general task list. This file
is the metadata slice only.

**Repos.** HSS worktree `.claude/worktrees/vault-readonly-slice-review-0f7ee7` (branch
`ai-claude/chat-findings-option-a-30464c`). Knowledge platform
`/home/ubuntu/projects/knowledge-platform` (branch `dev`). The authoring schema is in the second;
the database and API are in the first. **Most of these decisions change both.**

Nothing in this brief has been acted on. Its measurements were re-verified against the database
on 2026-08-14 — every field it calls empty is still empty — so the argument stands as written.

---

## 0. What actually exists today

Measured 2026-08-13 over 50 markdown notes; database counts re-verified 2026-08-14 at **49
rows**, the figure used below. The gap is one note written after the last import. (This table
read 48 until 2026-08-14 while `HANDOFF.md` read 49 — the two files had drifted, which is the
hazard this brief exists to avoid. The database is the tiebreaker.)

| Field | Schema | Populated | Where it lives |
| --- | --- | --- | --- |
| `tags` | `text[]`, GIN | **50/50 notes**, 3–5 each, 70 distinct | authoring + DB + embedding text |
| `facets` | `jsonb`, GIN `jsonb_path_ops` | **0/49** | DB + API only — **not in the authoring schema at all** |
| `related_ids` | `text[]`, no FK | **0/50** (key present, empty on every note) | authoring (`RelatedIDs`) + DB |
| `source_ids` | `text[]`, no FK | **0/49** on notes; **50 edges** on wiki pages | wiki frontmatter (`SourceIDs`) — markdown only |
| `aliases` | `text[]` | **0/49** | DB + API; **not in the Agent Note schema** |
| `summary` | `text` | **0/49** | DB + API; wiki pages have it, notes do not |
| `frontmatter` | `jsonb` catch-all | **0/49** | DB only |

So: **`tags` is the only metadata the corpus actually carries.** Everything else is schema
waiting for a decision. That is the single most important fact in this document — none of these
choices are constrained by existing data, and all of them get harder once they are.

### The tag census

70 distinct tags over 50 notes. The head:

```
gotcha 25   unity 11   postgres 9   git 7   tooling 7   windows 7   testing 6
embeddings 5   dedup 5   rag 5   configuration 5   pytest 5   knowledge-vault 5
retrieval 5   reference 5   mcp 4   editor-tooling 4
```

**40 of the 70 appear exactly once.** That long tail is the signal: there is no controlled
vocabulary, and nothing has ever rejected a new tag.

Three groups are visibly doing different jobs:

- **Topics** — `postgres`, `unity`, `pytest`, `pgvector`. Genuine subject matter.
- **Projects** — `hss` (3), `b2-migration` (3). These are the ones ADR 0017 was written for.
- **Kinds** — `gotcha` (25, half the corpus), `reference` (5), `design` (1). Not what a note is
  *about*; what a note *is*. Neither a topic nor a project.

That third group is the finding that should drive the discussion, and it is not what anyone
expected to find.

---

## 1. Decision: does `tags` stay in the embedding text?

**Status: SETTLED 2026-08-15 by measurement — tags stay. Decisions 2 and 3 are unblocked.**

> The counterfactual has been run on the full corpus
> (`python -m scripts.measure_dedup_similarity --tag-counterfactual`, 49 notes, 1176 pairs per
> arm, both arms embedded fresh). **Removing tags does not open a usable band — it widens the
> overlap**, from −0.0818 to −0.0950. Dropping them lowered the floor by 0.0109 but the ceiling
> by 0.0242: the reference pairs' overlapping tags are genuine signal for restatement, which is
> what the ceiling measures, so tags help the true-positive side more than they cost the
> false-positive side.
>
> **The reasoning below was sound and its conclusion was wrong**, which is why it is kept rather
> than deleted. It extrapolated from a 14-note sample where tags moved the maximum pair −0.0513,
> and inferred that the floor would fall far enough to open a gap. On the full corpus the floor
> did fall — just not nearly as far as the ceiling did. The one-sided measurement was the error,
> the same shape of error the calibration procedure itself exists to prevent.
>
> A second finding matters more than this decision. The floor has risen from 0.7406 to 0.8318
> because the corpus gained a note that *refutes* an earlier one, and cosine similarity cannot
> tell a refutation from a restatement — they share vocabulary, subject and tags while asserting
> opposite things. A corpus that records its own changes of mind will keep producing such pairs,
> so the floor drifts away from any usable threshold as the corpus matures. See
> `app/vault/docs/embedding-calibration.md`. **The remaining lever is a different model, not a
> different text shape.**

The original argument, preserved:

ADR 0013 embeds `title + aliases + tags + summary + body`. ADR 0016's amendment then measured
what tags do to the similarity distribution, and it is not small:

- Adding one shared tag to ten notes raised **all 45 pairwise cosines**: mean +0.0385, max +0.0825.
- Removing tags from a 14-note sample moved the *mean* pair −0.0099 but the *maximum* pair
  **−0.0513**, and one pair sharing three tags fell **−0.0995**.

Tags inflate the **top** of the distribution, which is exactly what sets the dedup floor. The
measured floor-to-ceiling margin is **0.0094** (floor 0.7406, corpus max; ceiling 0.7500, minimum
over hand-written duplicates). Tag inflation is roughly **5x the entire margin**.

`flag_at` is therefore pinned at 1.0 — only an identical embedding flags — and it will stay
pinned until the margin separates. **Removing tags from the embedding text is the single largest
lever on whether semantic dedup can ever be calibrated.**

The counter-argument is real: tags are genuine topical signal, and they are the only structured
topicality the corpus has. Dropping them from the embedding costs semantic ranking quality in
search.

**The measurement that decided it, run 2026-08-15 — see the box above:** the counterfactual on all
not the 14 sampled. Score every pair with tags in the embedding text and again with tags removed;
report floor, ceiling, and margin for both. `scripts/measure_dedup_similarity.py` already runs
both sides — the `--tag-counterfactual` flag added on 2026-08-15 does exactly this. If removing
tags opens a margin above `MINIMUM_SEPARATION` (0.05), semantic dedup becomes possible and that is
probably worth more than tag-weighted ranking. If it does not, tags are not the blocker and this
decision goes away.

**It did not, so the decision has gone away.** Decisions 2 and 3 no longer wait on it: they change
which strings land in the embedding text, and the answer is that the embedding text is not where
the problem lives. Judge them on queryability alone — which is what facets were for — and treat
any effect on the dedup margin as a rounding error against a −0.08 overlap.

---

## 2. Decision: which tags become facets, and is `FACET_NAMES` the right set?

**Status: `FACET_NAMES` is a closed set of three. The corpus suggests it is missing one.**

`app/vault/facets.py` ships `{"project", "area", "system"}` — closed on purpose, because an
unrecognised name is more likely a typo (`projects`) that silently files a note where nothing
looks for it. Values inside each name are open.

The obvious migration is `hss` and `b2-migration` → `project`. That is 6 tag occurrences across
at most 6 notes, and it is uncontroversial: they are project names sitting in a field that feeds
the dedup gate.

**The interesting case is `gotcha`.** It is on 25 of 50 notes — half the corpus — and it is not a
topic, a project, an area, or a system. It is a *kind*: this note records a trap. `reference` (5)
and `design` (1) are the same axis. Under the current `FACET_NAMES` there is nowhere to put them,
so they stay tags, keep feeding the embedding, and keep inflating the pairs that share them —
and a tag on half the corpus inflates a very large number of pairs.

Three options:

- **(a) Add a `kind` facet name.** Moves `gotcha`/`reference`/`design` out of the embedding.
  Biggest reduction in tag inflation available, because it is the largest tag. Cost: `kind`
  overlaps conceptually with `doc_type` (`Agent Note`, `Wiki Page`), which is a *different* axis —
  document genre in the governance schema vs. content genre. Naming them both "kind" will confuse
  someone within a month; consider `genre` or `note_kind`.
- **(b) Leave them as tags.** Accepts the inflation. Defensible only if decision 1 removes tags
  from the embedding entirely, in which case the inflation stops mattering and this whole
  question is cosmetic.
- **(c) Promote to `doc_type`.** Wrong — `doc_type` is governed by `types.yml` folder globs, and
  `Agent/notes/**` is constrained to exactly `Agent Note`. Changing that reaches into the
  governance layer for a content property. Do not.

**Depends on decision 1.** If tags leave the embedding text, (b) becomes nearly free and the
facet migration is only about queryability.

**Cost note:** moving a tag to a facet changes the note's embedding text, so it requires a
**re-embed of every affected document**. That is a real data operation with an API cost, not a
metadata tidy. At 25 notes for `gotcha` it is small; do it in one pass, not incrementally.

---

## 3. Decision: is there a controlled vocabulary for tags?

**Status: no control exists. 40 of 70 tags are singletons.**

Facet *names* are closed and facet *values* are open. Tags are open in both senses — nothing
validates them, so `editor-tooling` and `tooling`, or `git` and `git-worktree`, coexist without
anyone deciding they should.

Options, in increasing order of enforcement:

- **Nothing.** Tags stay folksonomy. Fine if decision 1 removes them from the embedding, since
  then they only affect the lexical arm and a GIN filter, where a long tail is harmless.
- **A census surface.** `GROUP BY unnest(tags)` as an endpoint — trivial to build, already noted
  in `HANDOFF.md` §2 as probably the real grouping surface. Makes drift *visible* without
  enforcing anything. **Cheapest useful move; do this regardless of the other decisions.**
- **A validated vocabulary** in governance YAML, mirroring `FACET_NAMES`. Rejects unknown tags at
  the write boundary. Highest cost: someone maintains the list, and every new topic is a
  governance change. Probably wrong for a corpus this size.

---

## 4. Decision: what the edge graph is for

**Status: half the graph exists in markdown, none of it is in the database.**

This is the part with the most latent value and the least written down.

### Edges that exist today

| Edge | Count | Keyed by | Stored |
| --- | --- | --- | --- |
| wiki page → note (`SourceIDs`) | **50** | note ID (32-hex, stable) | wiki frontmatter only |
| wiki page → wiki page (`Related`) | **21** | **page title**, as `[[Wikilink]]` | wiki frontmatter only |
| note → note (`RelatedIDs`) | **0** | note ID | schema only, never populated |

Two consequences.

**The wiki graph is complete and unused.** All 50 notes are cited by exactly one page — that is a
full partition, verified by `check-wiki` reporting zero uncovered notes. It is a real
synthesis-to-source provenance graph, and nothing can query it, because **no wiki page is in the
database**. `vault_document_kind` reserves `wiki`, but
`vault_documents_compile_provenance_consistent` requires `compile_run_id`, `compiled_by` and
`compiled_at` NOT NULL for `kind='wiki'`, and `vault_compile_runs` is empty. Projecting the wiki
layer is a prerequisite for traversing anything.

**`Related` edges are keyed by title, not ID.** `[[RAG and Retrieval Design for the B2 Engine]]`
breaks the moment a page is retitled, and the compile engine has retitled pages before. Every
other edge in the system is ID-keyed. **This is the one existing inconsistency worth fixing
before the graph is projected**, because a title-keyed edge that lands in the database becomes a
broken foreign key nobody notices.

### The question to answer first

**What traversal does anything actually need?** The schema can support many shapes and the right
storage depends entirely on the queries. Candidates, roughly in order of plausible value:

1. *"What sources produced this synthesized claim?"* — wiki → notes, one hop. Provenance. This is
   the query the vault's whole design premise rests on, and it is the one the data already
   supports.
2. *"What else did this note contribute to?"* — note → wiki, the reverse. Needs the same edge,
   read backwards.
3. *"What notes are adjacent to this one?"* — note → note. **No data exists.** Would have to come
   from co-citation (two notes cited by the same page — derivable from the 50 existing edges for
   free), from shared facets, or from embedding similarity. Co-citation is the cheapest and is
   already implicit.
4. *"What is near this in concept space?"* — that is vector search, not a graph, and it exists.

Note that (3) via co-citation needs **no new authoring**, no schema change, and no human effort:
the 14 pages already partition the corpus into topical clusters. That may be the entire graph
anyone needs.

### Storage, once the queries are known

- **Keep `text[]` + GIN.** Zero migration. Fine for one-hop lookups in either direction. Awkward
  for multi-hop: a recursive CTE over an array column works but reads badly and cannot be indexed
  as an edge.
- **A dedicated edge table** `(from_id, to_id, kind)` with an FK to `vault_documents` and indexes
  both ways. Right answer if multi-hop traversal or edge attributes (weight, provenance, when it
  was asserted) ever matter. Costs a migration and a projection step.
- **Both** — arrays as the authoring surface, an edge table as a derived projection rebuilt on
  compile. Sound, and the most work.

**Constraint that is already settled: `related_ids` and `source_ids` carry no foreign key, on
purpose.** ADR 0012 states why — a contribution may legitimately reference a note that is
archived, flagged, or not yet written, and human-layer ids are unstable across a
rename-plus-edit. If an edge table is introduced it must either preserve that tolerance
(nullable target, or no FK) or the write path has to start rejecting forward references. Decide
that explicitly; it is the kind of thing that gets discovered at 2am.

---

## 5. Decision: do notes get `summary` and `aliases`?

**Status: both are in the DB and the API contract, in the authoring schema for wiki pages only,
and empty on every note.**

Both are in the embedding text (ADR 0013), so both are levers on the same distribution decision 1
is about. `aliases` is described there as "the strongest case in the whole schema" — a note
titled "PostgreSQL" aliased "Postgres" is findable by someone who types the other name.

For notes specifically, the question is whether an agent writing a note can be trusted to produce
a *useful* summary and useful aliases, or whether it will produce a restatement of the title that
adds tokens to the embedding without adding signal. A bad summary is worse than none: it is
extra text in the vector, pulling notes with similarly-bad summaries together.

If they are added, add them to `Vault/00 Governance/Schemas/types.yml` under `Agent Note` as
`recommended`, not `required` — the same conservatism the schema already applies, since the
back-catalogue of 50 notes has neither.

---

## 6. Sequence

The dependencies run one way. Doing these out of order means redoing them.

1. ~~Measure the tag counterfactual~~ and ~~decide tags-in-embedding~~ (decision 1). **Both done
   2026-08-15: tags stay.** Removing them widened the overlap rather than opening a band, so the
   embedding text is not where the dedup problem lives and nothing below is contingent on it.
2. **Build the tag census endpoint** (decision 3, middle option). Independent of everything, makes
   the vocabulary visible, cheap — and now the cheapest remaining step, since step 1 is done.
4. **Decide `FACET_NAMES`** (decision 2) on **queryability alone**. The "what tags cost the dedup
   margin" input is gone: against a −0.08 overlap, moving a tag to a facet changes nothing that
   matters. Add the `kind`/`genre` axis or
   consciously decline it.
5. **Extend `types.yml`** so notes can carry facets — and `summary`/`aliases` if decision 5 says
   so. **Nothing can be backfilled before this**; the authoring schema is the constraint, not the
   database.
6. **Re-annotate the corpus** through the engine. 50 notes, and it is the only step that needs
   human judgment per note.
7. **Teach the importer to PUT** (`ADR 0018`) and backfill. Facets-only edits cost no embedding
   call by design; a tag migration does, and needs a re-embed pass.
8. **Fix `Related` to be ID-keyed**, then project the wiki layer into `vault_documents` with its
   `vault_compile_runs` rows (decision 4). Traversal becomes possible here and not before.

Step 1 is done. **Step 5 is the real gate** — the handoff's task 2e records that the backfill is
blocked on data, and this is that data. Step 2 remains the cheapest way in.

---

## 7. Settled — do not re-litigate

- **Facets are not embedded.** ADR 0017, structural rather than a strip rule, because a reserved
  prefix inside the tag list makes the guarantee conditional on string content.
- **Facet shape is a database CHECK; facet vocabulary is application code.** ADR 0009's precedent.
  Adding a project is a data change, not a migration.
- **`jsonb_path_ops`, not `jsonb_ops`.** Containment only; the existence operators would need a
  different index, not a widened one.
- **Facet values are always arrays**, even for one value, so a containment query cannot silently
  miss a scalar.
- **`doc_type` is governed by folder globs** in `types.yml`. `Agent/notes/**` is `Agent Note`.
  Content genre is not `doc_type`.
- **`vault_path` is the only policy key** (ADR 0010), and for agent rows it is synthetic —
  `Agent/notes/<uuid>.md`, which names no real file. Do not treat it as a join key to markdown.
- **Obsidian reserves lowercase `tags`, `aliases`, `cssclasses`.** If the vault standardizes other
  frontmatter keys on PascalCase, these three stay lowercase as documented exceptions.
