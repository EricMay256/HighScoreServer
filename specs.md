# specs.md — Validated runs, cumulative scoring, and Alembic adoption

## Agent execution guardrails (READ FIRST)

These three rules gate how this plan is executed. They override any contrary reading of the phase descriptions below.

**1. Phase 1 only on this pass; hard stop for human review.** Execute **Phase 1 only** (the `0002` migration + `db/role.sql` grants + tests — no endpoints). Stop at the Phase 1 boundary and surface the work for review **before** starting Phase 2. Do not traverse multiple phases autonomously: the phases are loosely coupled and span schema, endpoint logic, a validator, and a C# client, which is too much surface to build before a human checkpoint. Work in the same small-unit-then-verify rhythm Phase 0 used.

**2. Grants live in `db/role.sql`, never in migrations — and must be re-applied after a table-adding migration.** Migrations run as the superuser `postgres` (via `.env`), which owns new objects, so the migration itself needs no grants. But the least-privilege tests connect as the restricted `leaderboard_app` role, which has **no** privileges on newly created tables (`runs`, `submission_idempotency`) until granted. So: (a) add grants for the new tables/sequences to `db/role.sql`; (b) after `alembic upgrade head`, **re-apply `db/role.sql`** to any database where the restricted role runs (dev, test) before those tests pass. A "permission denied for table runs" failure is a missing re-applied grant, **not** a code bug — do not fix it by moving grants into the migration (a `GRANT ... TO leaderboard_app` in a revision errors on prod, where that role does not exist).

**3. Never touch prod; never deploy.** Test migrations only against a **local throwaway** DB you create and drop (e.g. `createdb leaderboard_phase1_check` … `dropdb`), exactly as Phase 0 was verified. Any agent-run `alembic upgrade`/`downgrade` must have `DATABASE_URL` pointed at that throwaway — confirm the target first; never at a DB with real data, never at prod. The baseline `downgrade()` runs `DROP TABLE … CASCADE`, so running a downgrade against a real DB destroys data — throwaway only. Prod stamping, `git push heroku`, and the release-phase deploy are **human-only**; do not run mutating `heroku` commands, do not deploy, do not stamp prod.

---

## Goal

Enable richer, deeper game-mode flows without raising the floor for simple modes. Two new capabilities plus the schema-tooling change they force:

1. **Validated runs** — a mode can require clients to submit a full *run* (scenario version + seed + action log) instead of a bare score. The server validates the run and upserts the **server-computed canonical score**. The client's claimed score is never authoritative.
2. **Cumulative scoring** — a mode can accumulate score across submissions instead of keeping a personal best.
3. **Alembic adoption** — forced by this work (first time existing prod tables are altered).

**Compatibility posture (corrected from an earlier overstatement that this is a hard breaking change):** the server changes are **wire-additive and backward-compatible** for existing clients on `required_tier=0` / `scoring_strategy='best'` modes — which, via the Phase 1 migration defaults, is every mode that exists today. Concretely: new response fields (`validated`, `validation_tier`) are ignored by clients that deserialize with default settings (Newtonsoft drops unknown keys; the hss-unity `ScoreResponse` has no required-member enforcement); the new `idempotency_key` is only *required* on cumulative modes, so raw/best submissions are unaffected; and `/runs`, `RunSubmission`, `scores.run_id`, and the new `game_modes` columns are all additive. The granted breaking-change latitude applies to **the SDK's own public API** when it's extended to consume runs/cumulative (e.g. new C# fields may be non-nullable since the upgraded server always sends them) — not to the server wire contract. **The only client-visible break is a deliberate one: reconfiguring an *existing* mode to `required_tier > 0`**, which makes the old raw `SubmitScore` return a 409 that pre-run clients can surface but not auto-route.

"Floor stays accessible" means exactly two things: (a) `required_tier=0`, `scoring_strategy='best'` modes keep the current `{score, game_mode}` flow with no new *required* request fields; (b) every schema change is additive with safe defaults so existing rows/modes need no backfill.

## Decisions already made (do not re-open)

- **Two endpoints, not one.** `POST /api/leaderboard/scores` (raw) and `POST /api/leaderboard/runs` (validated). Rationale: a run is a distinct resource that *produces* a score; the two paths have very different cost/latency/rate-limit profiles; `/docs` stays self-documenting.
- **Cross-routing returns a guided 409** with a machine-readable hint (`code`, `submit_to`), not just prose.
- **Action log stored as a single compressed blob** on `runs` (gzipped JSON in `BYTEA`), **not** a normalized per-action table — Heroku Postgres row economics.
- **Tiered validation (0–3).** Tier 3 (deterministic replay) is real, not hypothetical. Schema and endpoints are tier-agnostic; the mode declares a *minimum* required tier; the run records the tier actually achieved.
- **Cumulative is gated by idempotency keys, not by validation.** Cumulative may apply at any tier; it only requires an idempotency key (the dedup mechanism).
- **Adopt Alembic now, with raw-SQL migrations** (`op.execute`, no ORM models, no autogenerate). This does **not** commit the project to asyncpg — async stays deferred.
- **Unified `ScoreResponse`** for both paths, gaining `validated: bool` and `validation_tier: int` (0 for raw). Breaking; C# models update in lockstep.

## Non-goals (explicit)

- asyncpg / async DB migration — deferred (schema tooling and runtime driver are separable).
- Server-issued seeds (anti-grinding handshake) — future tier; design the `seed` field aware it may become server-issued, but do not build the handshake.
- Normalized `run_actions` analytics table — blob is used; revisit only if SQL-level per-action analytics is genuinely needed.
- Admin review surface / anomaly tooling — optional later, not this work.
- Password reset, React integration of runs — out of scope.

## Repo facts to verify before coding

The agent has the live repo; **confirm these against the code, do not trust this document** (the author's reference snapshot was partly stale):

- `app/leaderboard_routes.py` → `submit_score`: mode lookup, `requires_claimed_account` gate, the per-period upsert loop with the improvement predicate (`_is_improvement_predicate`), cache invalidation loop, `_fetch_score_with_rank`. **Extract the write tail into a shared helper.**
- `app/models.py` → `ScoreSubmission`, `ScoreResponse`.
- `db/schema.sql` → `game_modes` columns (expect `name`, `sort_order`, `label`, `requires_claimed_account`); `scores` columns and the UNIQUE constraint.
- `db/role.sql` → grants for `leaderboard_app`.
- **Whether a deterministic replay core exists in-repo and in what language** — this decides the tier-3 binding (see Validator).

---

## Phase 0 — Alembic adoption (COMPLETE before this spec is handed off)

**Status: done by the maintainer, outside the Claude Code workstream.** Alembic is already introduced, the baseline revision is authored and verified byte-equivalent to production (public schema), and the live databases are stamped. This section documents the finished state so the agent does not re-derive or re-run any of it. **Phase 1 begins at revision `0002`.**

What exists now:
1. `alembic` is a dependency; `python-dotenv` is a direct dependency (env.py loads `.env`). `alembic.ini` leaves `sqlalchemy.url` blank; `migrations/env.py` reads `DATABASE_URL` from the environment (loading `.env` with `override=False`, so an explicit process-env URL — for a prod stamp or a throwaway — wins over `.env`). Migrations are raw SQL (`op.execute`); **no SQLAlchemy models, no autogenerate.**
2. **Baseline `0001_baseline`** reproduces the live `public` schema exactly: `game_modes`, `users`, `refresh_tokens`, `scores`, their `serial` sequences, all constraints (with production's exact names and the `sort_order` CHECK in production's `varchar`-array form), both `idx_scores_lookup_*` covering indexes including the `score DESC` member, and the partial unique `idx_users_email`. Verified by building a fresh DB with `alembic upgrade head`, schema-dumping it, and diffing against a production `--schema-only --no-owner --no-privileges -n public` dump until only ignorable noise remained.
3. **Heroku platform objects are deliberately excluded from the baseline** — the `_heroku` schema and its functions, the `pg_stat_statements` extension, and the extension-management event triggers are platform-provisioned, not application schema, and must never be recreated locally. A fresh `alembic upgrade head` builds the application's `public` objects and nothing else. (When comparing future dumps, this noise plus `alembic_version`, the `CREATE SCHEMA public`/COMMENT preamble, and the `\restrict`/`\unrestrict` tokens are expected and ignorable.)
4. **Stamping:** every pre-existing database (production and local dev) was marked `alembic stamp 0001_baseline` — never `upgrade` — because the objects already exist there. Only fresh/empty databases (CI, a new contributor's local) run `alembic upgrade head` to build from scratch.
5. **Source of truth:** migrations are now canonical for schema change. `db/schema.sql` is to be retained only as a labeled bootstrap snapshot (the relabel is a pending doc task — see below).

**Implication for the agent:** Phase 1 adds a new revision on top of `0001_baseline` (i.e. `0002_*`). Running `alembic upgrade head` against an already-stamped database applies only new revisions and never re-runs the baseline. Do not modify `0001_baseline`.

**Deploy-time migrations — decided: release-phase.** The `Procfile` carries `release: alembic upgrade head`, so every deploy auto-applies pending migrations before the new release goes live; a failing migration aborts the deploy (the intended safety). This was added *after* prod was stamped at `0001_baseline`, so its first run was a no-op. Phase 1's `0002` and later revisions apply automatically on deploy.

### Documentation — OUTSTANDING on the dev branch (not yet done)

The Alembic code is implemented and committed; the supporting documentation is not. Complete these before merging the dev branch / handing off, so the repo's docs match its behavior:

- [ ] **`db/schema.sql`** — add a header comment relabeling it a bootstrap snapshot, NOT the source of truth (schema changes go through Alembic revisions). Suggested header is in the chat handoff.
- [ ] **`README.md`** — add a "Database & migrations" section (fresh DB → `alembic upgrade head`; pre-existing DB → `stamp 0001_baseline` once; `DATABASE_URL` from env/`.env`; Heroku auto-migrates via the `release:` phase). Suggested text is in the chat handoff.
- [ ] **`CLAUDE.md`** — run-docs already reflect Alembic + stamp-vs-upgrade + `.env`/override + release-phase deploy (in the handoff copy); fold the same into the repo's `CLAUDE.md` if it diverged.
- [ ] **`Procfile`** — confirm the `release: alembic upgrade head` line is committed alongside the `web:`/`worker:` entries (this is implementation, but verify it landed).

---

## Phase 1 — Schema changes (migration only; no endpoints)

One or more Alembic revisions, authored on top of the stamped `0001_baseline` (i.e. `0002_*` onward). All additive with safe defaults.

### New table: `runs`
- `id` PK; `user_id` FK→`users` `ON DELETE RESTRICT`; `game_mode` FK→`game_modes(name)`.
- `scenario_version INT NOT NULL`; `seed BIGINT NOT NULL`.
- `claimed_score BIGINT NULL` (recorded, never trusted); `canonical_score BIGINT NULL` (null until validated).
- `validation_tier SMALLINT NULL` (tier actually achieved); `status` — `pending | validated | rejected`.
- `client_run_id TEXT NOT NULL` (idempotency + anti-replay); `actions BYTEA NOT NULL` (gzipped JSON action log); `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- `UNIQUE (user_id, game_mode, client_run_id)` — anti-replay / idempotency for runs.
- Optional index `(game_mode, status, created_at)` for debug/admin lookups.

### New table: `submission_idempotency`
For cumulative dedup on **raw** (tier-0) modes, where there is no `runs` row.
- `(user_id, game_mode, key)` `UNIQUE`; `first_seen TIMESTAMPTZ`.
- Write via `INSERT ... ON CONFLICT DO NOTHING`; a conflict means duplicate ⇒ the submission is a no-op returning the current score.
- For **run** submissions, `runs.client_run_id` already provides this — the agent may reuse `runs` uniqueness instead of double-writing here; if so, document the decision.
- **Unbounded growth:** add `scripts/prune_idempotency_keys.py` mirroring the existing prune scripts. Retention window is a decision (default suggestion: 90 days, accepting that an ancient replay could double-count — acceptable for a game leaderboard). **Create the stub + note; implementation deferred.**

### Alter: `game_modes` (the reason Alembic is needed)
- `required_tier SMALLINT NOT NULL DEFAULT 0` — `0` = raw via `/scores`; `>=1` = run required via `/runs`.
- `scoring_strategy VARCHAR NOT NULL DEFAULT 'best' CHECK (scoring_strategy IN ('best','cumulative'))`.
- `game_key TEXT NULL` + `CREATE INDEX ... ON game_modes (game_key)` — **forward-provisioning, no behavior this phase.** Tags a mode with the game that owns it. Nullable: existing modes (`precision`/`blitz`/`flood`) have no game and need no backfill. **Nothing reads it in this feature set** — its intended future use is scoping aggregate endpoints (e.g. `/latest`) so a game lists only its own modes' leaderboards instead of every registered mode. Included here solely to avoid a second `game_modes` alter later. No FK and no `games` table now (a likely-eventual but deferred modeling decision); a plain index suffices for future `WHERE game_key = ?` filtering. (This reverses the earlier "server-side mode ownership — deferred" note, by the maintainer's decision, folding the column in while the table is already being altered.)
  - **Expose, don't enforce:** surface `game_key` read-only in the `GameModeConfig` API response and the C# model (nullable), exactly as `required_tier`/`scoring_strategy` are surfaced, so clients *can* read it. Do **not** add ownership filtering/enforcement logic this phase — that's a future change against `/latest` and friends.

### Alter: `scores`
- `run_id INT NULL REFERENCES runs(id)` — links a leaderboard row to the run that produced it (validated modes); null for raw.

### Grants and the migration/grant split (read carefully — prod-deploy-breaker if ignored)

**Migrations carry structural DDL only** (tables, columns, constraints, indexes, sequences). **Grants do NOT go in Alembic revisions.** They live in `db/role.sql`, applied per-environment.

Why this split is mandatory here — the dev/prod role asymmetry:
- **Dev** uses least-privilege roles: a restricted `leaderboard_app` runtime role that genuinely lacks rights until `db/role.sql` grants them. A missing grant **fails loudly in dev** — dev is the stricter gate that validates the grant layer.
- **Prod** runs as a single owner role (the Heroku-provisioned user). In PostgreSQL an object's owner implicitly holds all privileges on it, so explicit grants are irrelevant in prod — a missing grant **silently passes**.

Consequences for the migration:
- A `GRANT ... TO leaderboard_app` inside a revision **errors in prod** with `role "leaderboard_app" does not exist`, because prod has no such role. Under release-phase migrations that **aborts the deploy.** Keep all `GRANT`/`REVOKE` out of revisions.
- `db/role.sql` is **dev-enforced and prod-documentary**: prod never executes it, but maintain it accurately so the least-privilege posture is portable the day prod gains a restricted role. (Whether Essential-tier Heroku Postgres currently permits `CREATE ROLE` / additional credentials to mint `leaderboard_app` in prod is **unverified — confirm before assuming.**)

What `db/role.sql` should grant for this feature (dev): `GRANT SELECT, INSERT, UPDATE ON runs, submission_idempotency` + sequence-usage grants to `leaderboard_app`; `DELETE` only on tables touched by prune scripts, matching how `prune_guests` is privileged.

**Acceptance:** revisions contain structural DDL only (no grants); `alembic upgrade head` runs clean in a single-role environment; in dev, `db/role.sql` grants `leaderboard_app` exactly what the app needs and no more; no endpoint or behavior change yet.

---

## Phase 2 — Models + shared write tail + cumulative + cache fix

### Pydantic (`app/models.py`)
- **The action log is opaque at the API boundary — do NOT define a typed `RunAction` in `app/models.py`.** `actions` is a JSON array validated for **transport bounds only** (max element count, max decompressed byte size, must be an array), never internal semantics. Use `actions: list[Any]` — this commits only to "a sequence of events" and presumes nothing about element shape (which may be objects or compact arrays). `list[dict[str, Any]]` is a safe tightening *only* if every scenario's actions are known to be object-shaped; default to `list[Any]`.
- `RunSubmission` — `game_mode`, `scenario_version (ge=1)`, `seed`, `actions: list[Any]` (opaque, bounded), `claimed_score: int | None`, `client_run_id: str (min_length=8)`. The server stores `actions` as the gzipped blob regardless of shape; `scenario_version` is the key a future validator uses to select the correct parser.
- **The typed action shape is deferred and owned per-scenario by its validator, not by the shared API layer.** See "Deferred: typed action shape" below.
- Widen `ScoreResponse` (**breaking**): add `validated: bool`, `validation_tier: int`. Raw path returns `validated=false, validation_tier=0`.
- `ScoreSubmission`: add `idempotency_key: str | None`. **Required when the target mode's `scoring_strategy='cumulative'`** (validate inside the handler against the looked-up mode, since the requirement is data-dependent — don't try to express it purely in the model).

### Deferred: typed action shape

The concrete shape of an action is **deliberately not defined now.** A single global `RunAction` is likely the wrong abstraction — different modes/scenarios (and future non-Flick-Fest clients) log different event vocabularies that have no reason to agree. The shape lives next to the validator/replay code for a given `scenario_version`, not in shared API models.

This is safe with **no migration debt**: because `scenario_version` is in the envelope and the stored blob is opaque, a new shape is just a new `scenario_version` + a new parser. Old runs keep their blob and their old parser; nothing is locked in by today's non-decision.

**Pin a scenario's shape only when one of these triggers fires (per scenario, not globally):**
1. A mode is configured `required_tier >= 2` — score recomputation must read action semantics.
2. The deterministic replay is bound (Phase 4) — the replay core's input format *is* the contract; discover it, don't invent it.
3. Run emission is implemented in hss-unity for a real mode — producer (C#) and consumer (validator) must agree at that point.
4. A second consumer logs a different vocabulary — confirms per-scenario shapes were right; resist unifying.

**The existing sample:** store it as `tests/fixtures/runs/<scenario>_v1_sample.json`, labeled "sample, not contract," and anchor a Tier-1 bounds test against it. It informs later typing without being elevated to a committed model.

### Shared write tail
Extract from `submit_score` a helper both endpoints call. Inputs: `(user_id, game_mode, authoritative_score, sort_order, scoring_strategy, periods, run_id | None, idempotency context)`. Responsibilities: per-period upsert, cache invalidation, return the ranked `ScoreResponse`. **Single source of truth for write semantics.**

The upsert branches on `scoring_strategy`:
- `best` — existing improvement-predicate upsert (`SET score = EXCLUDED.score WHERE <improvement>`).
- `cumulative` — `SET score = scores.score + EXCLUDED.score` with **no** improvement gate. The submitted `score` is interpreted as the **increment** for this submission. Each period bucket accumulates independently (daily resets daily, alltime never resets) — this falls out of the existing `period_start` mechanic.

Cumulative requires idempotency: check the key first (`submission_idempotency` or `runs.client_run_id`); if seen, no-op and return the current score; if new, record it and apply the increment **in the same transaction** as the upsert.

### Cache
Fix the known `leaderboard:latest` invalidation gap while in this path: add `cache.delete("leaderboard:latest")` alongside the period-key invalidation loop.

**Acceptance:** raw `best` modes behave identically except the added response fields; a raw `cumulative` mode sums deduped increments per period and no-ops on a repeated `idempotency_key`; tests cover both strategies and the dedup no-op.

---

## Phase 3 — Endpoints + Validator (tiers 1–2 concrete, tier 3 wired-or-stubbed)

**409 body shape (constraint, not just a format preference).** Keep `detail` a **string** and put `code` / `submit_to` as **top-level siblings** of `detail`, via a custom exception/response (a bare `HTTPException(detail=...)` only emits `{"detail": ...}`, so a custom handler or a `JSONResponse` is required to add siblings). Do **not** nest `code`/`submit_to` *inside* `detail` as an object: the hss-unity client's `TryExtractDetail` only handles `detail` as a string or as a Pydantic-style array, and falls through to `detail.ToString()` for an object — surfacing stringified JSON in logs/UI. String `detail` + sibling fields gives the current client a clean human message *and* gives a future SDK machine-routable fields.

### `POST /api/leaderboard/scores` (raw) — tighten existing
- If `mode.required_tier > 0` → **409** `{ "detail": "<human message>", "code": "RUN_REQUIRED", "submit_to": "/api/leaderboard/runs" }`.
- Otherwise dispatch to the shared write tail (best or cumulative per strategy).

### `POST /api/leaderboard/runs` (new)
- Valid only when `mode.required_tier >= 1`; else **409** `{ "detail": "<human message>", "code": "RAW_ONLY", "submit_to": "/api/leaderboard/scores" }`.
- Pipeline: persist run (`status='pending'`) → `Validator.validate(run, required_tier)` → on success set `canonical_score`, `validation_tier`, `status='validated'`, then call the shared write tail with `authoritative_score = canonical_score` and `run_id` set → on failure set `status='rejected'` and return **422** with the reason.
- `client_run_id` UNIQUE gives idempotency/anti-replay: a duplicate submission returns the prior result without re-validating.
- **Rate-limit separately from `/scores`** — validation is the expensive path. (Flag: pick a limit; validation cost dominates.)

### Validator seam
Define an interface so the rest of the system builds without coupling to the replay core:

```python
class ValidationResult(BaseModel):
    canonical_score: int
    tier_achieved:   int
    status:          Literal["validated", "rejected"]
    reason:          str | None = None

class Validator(Protocol):
    def validate(self, run: RunRecord, required_tier: int) -> ValidationResult: ...
```

Tiers:
- **Tier 1 — bounds/shape:** `score <= per-scenario ceiling` (where declared), action-count and duration plausibility. Pure checks, no sim.
- **Tier 2 — score recompute:** re-derive the score from the action log via the **scoring rules** (not physics); confirm consistency with the claim. Needs the scoring formula server-side, not the full sim.
- **Tier 3 — deterministic replay:** run the existing deterministic replay over `seed` + `actions`; confirm the actions are valid and produce the score.

**OPEN — tier-3 binding (needs human input / repo inspection).** The deterministic replay exists; where it runs decides the wiring: (a) in-process Python port, (b) subprocess to a binary, (c) localhost sidecar service. The `Validator` interface isolates this. Build Tier 1 and Tier 2 concretely now. For Tier 3: if the replay core is in-repo and callable, wire it; otherwise implement `Tier3Validator` against the interface with a clearly-marked integration point and **surface the binding decision rather than guessing**. If a non-Python sidecar/binary is chosen, the Heroku polyglot-deploy story is unverified — flag it.

The mode's `required_tier` sets the minimum; the validator must achieve at least that and records `tier_achieved`, which allows re-validating old runs at a higher tier later with no schema change.

**Acceptance:** wrong-endpoint requests return the correct guided 409; a validated run computes the canonical score server-side and links it via `run_id`; claimed-score mismatch is recorded, not trusted; duplicate `client_run_id` is a no-op returning the prior score; tiers 1–2 have tests; tier 3 is exercised if bound, else its interface has a test double.

---

## Phase 4 — Tier-3 binding + Unity client

- Wire the deterministic replay per the resolved binding decision.
- C# `RunSubmission` + the scenario's typed action model (Newtonsoft); a Unity proof-of-concept that submits a run and receives a `ScoreResponse`. **This is where the action shape gets pinned for that scenario** (triggers 2 and 3 from "Deferred: typed action shape") — derive it from the replay core's input format, do not invent it. The C# emission must match the server's parser byte-for-byte in meaning — the determinism contract in miniature; watch enum/`StringEnumConverter` handling.
- Update C# `ScoreResponse` for the new fields (**breaking** — old clients must update).

**Acceptance:** C# models compile and round-trip the new shapes; the Unity PoC submits a run end-to-end and renders the returned score.

---

## ADRs to author (Nygard; number from current head)

Material decisions here that should be recorded. (I can draft these as full ADRs on request.)

1. Two submission endpoints over a polymorphic one — resource modeling + latency/rate-limit isolation.
2. Action log as a compressed blob over normalized rows — Heroku row economics.
3. Tiered validation with a mode-declared minimum tier — upgrade path without schema churn.
4. Cumulative scoring via idempotency keys — idempotency, decoupled from validation.
5. Alembic adoption with raw-SQL migrations — forcing function (altering deployed tables); async kept separate.

## Open decisions to resolve (collected)

- ~~Procfile release-phase migration vs. manual~~ — **resolved: release-phase** (`release: alembic upgrade head` in the Procfile; added after prod was stamped). No Phase 0 items remain open.
- Reuse `runs.client_run_id` for run idempotency vs. a unified `submission_idempotency` table for both paths (Phase 1).
- ~~`RunAction` concrete shape~~ — **resolved: deferred per-scenario** (opaque blob until a trigger fires; see "Deferred: typed action shape"). Not a blocker for Phases 1–3.
- Rate limit for `/runs` (Phase 3).
- Tier-3 binding: in-process port / subprocess / sidecar (Phase 3).