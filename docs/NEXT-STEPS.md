# Next steps

Current as of 2026-08-15. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF.md` holds the full task list and session
history; `HANDOFF-METADATA.md` holds the metadata-model decision brief. This file
exists so neither has to be read to know what to pick up.

**State:** branch `ai-claude/chat-findings-option-a-30464c`, 13 commits ahead of
`origin/dev` and unpushed, suite green (486), tree clean. `origin/dev` is 45+
commits ahead of `main`. Nothing since the last merge to `main` is configured on
Heroku, and nothing needs to be — see [Deployment](../README.md#deployment).

**Local databases are migrated to vault lineage `0007_write_scope_split`.** A
database that predates it needs `alembic -c alembic-vault.ini upgrade head`
before this code runs against it, or credential writes fail the old CHECK.

---

## 1. Merge to `main`

The highest-value step, and everything else is easier after it. Fully green, and
the longer it waits the more one merge carries.

- **Not a squash** (decided). A fast-forward brings the handoff documents'
  history — Windows paths, the private repo name — onto the public default
  branch permanently. That is accepted: no credential was ever committed in
  them, and they leave when they have served their purpose.
- **One silent behavioural change:** `main` hardcodes `max_size=10` per worker;
  the pool is now configurable and defaults to **4**. Nothing on Heroku
  overrides it, so merging applies it. It is a fix — 10 × 2 workers allocated
  the entire 20-connection limit, leaving nothing for the release dyno or for
  `pg:psql` during an incident — but it is a real reduction in concurrency and
  there will be no config change to point at later.
- Everything else deploys inert: `REQUIRED_ENV_VARS` is unchanged, Steam stays
  unavailable until its three variables are set, and the vault ships dark.

## 2. The search contract

The largest remaining piece, blocked on nothing, and it should start fresh —
three inputs have to be held at once. Detail in `HANDOFF.md` §2.

The fork worth deciding before writing code: **search-with-filters and
browse-by-filter are different operations.** "Show me everything tagged `unity`"
has no query to rank by. Either `q` becomes optional or a separate list endpoint
appears. A tag-census endpoint (`GROUP BY unnest(tags)`) is trivial and is
probably the real grouping surface — it is also step 2 of the metadata sequence,
so the two converge.

## 3. Extend `types.yml` so notes can carry facets

**The real gate on everything metadata-shaped.** `facets`, `aliases`, `summary`,
`related_ids` and `source_ids` are empty on all 49 rows — not because the write
path cannot set them, which it now can, but because the *authoring schema* has
nowhere to put them. Until this lands there is nothing to backfill and building
more write surface buys nothing.

The dependency that used to sit in front of this is gone: the tag counterfactual
is measured and tags stay in the embedding text, so decisions 2 and 3 are judged
on queryability alone.

## ~~4. Split `vault:write` into contribute / update / delete~~ — done 2026-08-15

**Shipped.** Vault ADR 0020, migration `0007_write_scope_split`. `vault:write`
is contribute only; `vault:update` and `vault:delete` gate replacement and
retirement. The migration widens the CHECK constraint and **grants nothing** —
re-granting privilege from a procedure that reruns would silently restore
permissions on every rebuild, rollback or staging refresh. Widening an existing
credential is a manual per-credential `UPDATE`, or a reissue. Credentials stay
non-expiring by default.

**One open call:** the three local `importer` credentials hold all four scopes
and were left alone. ADR 0020's own example of the shape this split exists for
is contribute + replace and *never* delete — but the importer lives in the
knowledge-platform repo, so whether it ever calls `DELETE /notes/{id}` was not
verified. Dropping `vault:delete` from them is a one-line `UPDATE` once that is
known.

<details>
<summary>The reasoning, kept for the record</summary>

Independent of everything above and small, so it can slot in whenever. Today
`require_write_scope` gates all three write routes on the single `vault:write`
scope, so **a credential that can add a note can also delete one** — including
the long-lived `importer` credential, whose actual need is contribute plus
update. ADR 0015 already establishes that scopes are verbs, so this is a
refinement of the existing model rather than a new idea; the tight `retire`
quota (10/min burst 5, deliberately the tightest, because a loop that deletes is
worse than a loop that writes) is the same instinct expressed at the wrong
layer.

**This needs an Alembic revision on the vault lineage.** `scopes` carries a
CHECK constraint enumerating the five known values
(`vault_agent_credentials_scopes_known`, `app/vault/tables.py`), so new scope
names cannot be issued until it is widened.

The fork to settle first, because it decides whether the migration is
data-touching:

- **`vault:write` keeps meaning "all writes"**, with `vault:update` and
  `vault:delete` as narrower grants. Non-breaking; existing credentials keep
  working. Muddier, and the permissive default survives.
- **`vault:write` narrows to contribute only.** Cleaner, and the version worth
  having. Breaking: every existing `vault:write` holder silently loses update
  and delete, so the migration must decide whether to grandfather them by
  granting the new scopes to current holders. Grandfathering is one `UPDATE` and
  keeps the importer working; not grandfathering means reissuing credentials.

Recommended: narrow it, and grandfather in the same revision — the whole point
is that *future* credentials get least privilege, and silently breaking the one
client that exists buys nothing.

**Not needed: a duration argument.** `scripts/issue_vault_credential.py` already
takes `--days`, which sets `expires_at`, and `app/vault/auth.py` refuses an
expired credential on every request with no cache to wait out. What is missing
is that nothing *defaults* to an expiry — omitting `--days` mints a permanent
credential.

**Decided 2026-08-15: leave that non-expiring.** These are machine clients an
operator revokes directly, revocation takes effect on the next request with no
cache to wait out, and an expiry that lapses unnoticed is an outage rather than
a security event. `--days` stays available for anything handed to a third party
or issued for one task.

</details>

---

## Closed recently, so nobody reopens them

- **Tags in the embedding text** — measured 2026-08-15 on the full corpus.
  Removing them *widens* the dedup overlap (−0.0818 → −0.0950); they help the
  duplicate side more than they cost the distinct side. Tags stay.
- **`flag_at`** stays 1.0, now because the bands genuinely **overlap** rather
  than merely touching. The floor rose 0.7406 → 0.8318 on one pair: a note and
  the later note that *refutes* it. Cosine similarity cannot separate a
  refutation from a restatement, and a corpus that records its own changes of
  mind keeps producing such pairs — so the floor drifts further from a usable
  threshold as the corpus grows. **The remaining lever is a different model, not
  a different text shape.** See `app/vault/docs/embedding-calibration.md`.
- **Sharing one DB connection between auth and handler** — rejected. It would
  pin a connection across the embedding call (up to 23s) on search, contribute
  and update. `app/vault/AGENTS.md` carries the rule.
- **Wiring `HSS_PROCESS_COUNT` to `WEB_CONCURRENCY`** — rejected. Heroku's
  buildpack sets it per dyno from CPU and RAM, so the connection budget would
  become a function of dyno size and resizing would stop the app booting.

## Known gaps, not yet scheduled

- **No review surface.** `vault:review` is granted by no route. Lower priority
  than it looks: at `flag_at = 1.0` the queue only fills on exact resubmission.
- **Two retire paths that do not talk to each other** — the markdown CLI refuses
  to retire a note a wiki page cites; the HTTP endpoint has no equivalent,
  because wiki pages are not in the database at all.
- **`python-jose` is still pinned** with CVE-2024-33663 open. The real comparison
  is python-jose vs PyJWT vs authlib; the vault's credential scheme is not a JWT
  implementation and is not a candidate.
- **Wiki pages cannot be stored** — `vault_documents_compile_provenance_consistent`
  requires a `vault_compile_runs` row, and that table is empty. Search therefore
  returns raw notes and no synthesis. This is a third sync path, distinct from
  agent contributions and ADR 0012's mark-and-sweep.
