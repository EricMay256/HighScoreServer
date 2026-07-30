# Spec: per-mode score ceiling (`game_modes.max_score`)

A per-mode score ceiling — applied to the raw submitted score on `/scores`
(tier 0) and to the server-computed canonical score on `/runs` — plus the
run-submission expectations it sits within. The surrounding validated-runs system
— two endpoints, the tiered validator, the `game_modes` config columns — is
already built and deployed. This spec adds the `max_score` ceiling (a nullable
`game_modes` column and the rule that reads it), a recorded-not-trusted
`claimed_tier` on runs, and the action-log conventions both rely on.

---

## Context (already built — this extends it)

Stated as the current state of the live system, so this spec reads on its own:

- **Two submission paths.** `POST /api/leaderboard/scores` is raw (**tier 0**) and
  trusts the submitted score as-is. `POST /api/leaderboard/runs` is validated: the
  client submits a `RunSubmission` (scenario version + seed + action log +
  `claimed_score`), and the server computes the **canonical score**. The claimed
  score is never authoritative.
- **`game_modes` config columns** are live: `required_tier` (the mode's minimum
  tier — `0` = raw via `/scores`, `>=1` = run via `/runs`), `scoring_strategy`,
  and `game_key`.
- **The validator** runs on the `/runs` path and returns a `ValidationResult`.
  Tier 1 is bounds/shape checks against `claimed_score`; tier 2 recomputes the
  canonical score from the action log. (Tier 3 deterministic replay is the
  strongest tier and is not touched by this spec.)
- **`MAX_SCORE`** is the global score ceiling.
- **Relevant Pydantic models** are live: `GameModeCreate` (the upsert/write
  model), `GameModeConfig` (the response model, built from trusted DB rows),
  `RunSubmission`, and `ValidationResult`.

---

## The change

### Schema — new nullable column

Add `max_score BIGINT NULL` to `game_modes`. `NULL` = inherit the global
`MAX_SCORE`; non-`NULL` caps the score for that mode. **`BIGINT`, not
`INTEGER`:** `MAX_SCORE` is ~1.8e11, well beyond int32's ~2.1e9, and the column
is compared against the `BIGINT` score columns (`scores.score`,
`runs.claimed_score`, `runs.canonical_score`) — an `INTEGER` ceiling near the
global cap would overflow.

> ⚠️ **Migration.** `game_modes` is already deployed, so this is a **new Alembic
> revision** on top of the current head — not folded into the original
> `game_modes` ALTER, which has shipped. The change is additive (nullable, default
> `NULL`, no backfill) and auto-applies on deploy via the release phase. **No new
> grant** is needed: `game_modes` is already granted to `leaderboard_app`, and a
> column-add inherits the table's existing privileges.

### Scope — `/runs` and `/scores`

`max_score` is enforced on **both** submission paths. On the validated path
(`/runs`) it is a **validation rule** inside the validator. `POST /scores` is
not validated, but the same per-mode ceiling applies there too: the route
compares the submitted score directly to `max_score`, extending score sanity to
the simple endpoint with no validator involved.

### Carrier — `ModeBounds`

The route reads the column and builds a pure `ModeBounds` value object passed into
`validate()`; the validator never touches the DB. Nullable fields fall back to
global ceilings, so unconfigured modes behave exactly as today.

```python
class ModeBounds(BaseModel):
    max_score:   int | None = None
    min_score:   int = 0
    max_actions: int | None = None

    @property
    def score_ceiling(self) -> int:
        return self.max_score if self.max_score is not None else MAX_SCORE

    @property
    def action_ceiling(self) -> int:
        return self.max_actions if self.max_actions is not None else MAX_RUN_ACTIONS
```

`validate()` gains a `bounds: ModeBounds | None = None` arg (defaulting to
all-global), so existing two-arg call sites and tests keep working unchanged.

Tier 0 game modes using the scores endpoint can directly compare the submitted score to `max_score`

### Enforcement subject differs by tier

One ceiling invariant, three subjects (the canonical score on the validated
tiers; the raw submitted score at tier 0):

- **tier 0** — the raw submitted score is compared directly to `max_score`;
  over-ceiling is rejected with a 422.
- **tier 1** — checked against `claimed_score` (which *becomes* canonical).
- **tier 2** — checked against the RECOMPUTED canonical score. An over-ceiling
  recompute is a REJECTION, not a clamp: it signals a scorer bug or a too-low cap
  and must surface, not be silently truncated.
- Out-of-range → 422, same channel as other run rejections.

### Claimed tier vs. achieved tier

`RunSubmission` carries `claimed_tier: int | None` — the tier the client asserts
its log supports. Like `claimed_score`, it is **recorded, never trusted**:
persisted on the run, but the validator independently records `tier_achieved` =
the tier it actually reached. A `claimed_tier > tier_achieved` mismatch is a
recorded signal (the client over-claimed its log's fidelity), not grounds to trust
the claim — the achieved tier is authoritative. When omitted, validation targets
the mode's `required_tier`.

This matters to the ceiling at tier 2: a run on a tier-1 mode that carries a
scoring-grade log can claim `claimed_tier=2`, flagging it as an upgrade candidate
without changing what's enforced today.

> ⚠️ **Migration.** Persisting `claimed_tier` adds `runs.claimed_tier SMALLINT
> NULL`. Both this and `game_modes.max_score` are not-yet-shipped column adds, so
> they can ride a **single** new Alembic revision rather than two. (This is a
> legitimate bundling of two pending changes — distinct from the stale "fold into
> the original ALTER" framing, which was moot because that ALTER had shipped.)

### Tier-1 action blob — recommended convention

Tier 1 checks bounds/shape (`score <= ceiling`, action count) but never reads
action *content*, so the blob's element shape is unconstrained at tier 1. That
raises a real question — what should a client actually put in it? Two reasonable
conventions:

- **(recommended) Emit the same scoring-grade log you'd emit for tier 2, even on
  tier-1 modes.** The game loop already produces the events; the marginal cost is
  compression + upload. This keeps **one** client emission path across all tiers,
  provides a forensic record, and — the real payoff — makes the run upgrade-able:
  raising the mode to tier 2 later re-validates banked runs with no re-collection.
  Pair it with `claimed_tier=2` to mark the run as an upgrade candidate.
- **(pragmatic shortcut) Emit a minimal well-formed blob** (e.g. a compact summary
  array) for modes you are certain will never go tier 2. Cheaper on the wire, but
  the run is permanently tier-1-bounded and carries no forensic detail.

### Admin round-trip

Thread `max_score` through `GameModeConfig` / `GameModeCreate`, the `POST
/game_modes` upsert (column list / `VALUES` / `ON CONFLICT DO UPDATE` /
`RETURNING`), `list_game_modes`' SELECT, and both `GameModeConfig(...)`
constructions — so caps are tunable at runtime with no redeploy. Mechanical;
mirrors how `required_tier` is threaded.

### Axis

Keyed on `game_mode` (config axis), NOT `scenario_version` (validator axis). A
scenario-derived ceiling (max achievable given a scenario's layout) is a separate,
deferred concern keyed on `scenario_version`.

### Deferred

`min_score` (a floor on the canonical score) is accommodated by `ModeBounds` but
unenforced. The max-duration bound is also deferred — it needs the typed action
semantics to read elapsed time from the log (see **Action-log expectations**
above).

---

## Acceptance

- a tier-0 raw score above `max_score` (via `/scores`) → 422 `"Invalid Score"`
- a tier-1 run claiming above `max_score` → 422 `"claimed_score exceeds maximum N"`;
- a tier-2 recompute above `max_score` → 422 `"recomputed score exceeds maximum N"`;
