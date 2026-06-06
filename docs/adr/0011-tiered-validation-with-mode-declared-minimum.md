# 12. Tiered validation with a mode-declared minimum tier

Date: 2026-05-31

## Status

Accepted

## Context

Different game modes need different levels of trust in a submitted score. A
casual mode is fine accepting the client's number; a competitive mode wants the
server to recompute or fully replay the run before believing it. Validation
strength is therefore not one thing — it is a spectrum with very different costs:

- bounds/shape checks — cheap, no simulation;
- score recompute from the action log — needs the scoring rules server-side;
- deterministic replay over seed + actions — needs the full simulation core.

Two questions had to be answered: how to express the spectrum, and where to set
the requirement.

## Decision

Adopt **four tiers (0–3)** with the **mode declaring a minimum required tier**
and the **run recording the tier actually achieved**.

- `game_modes.required_tier` (default 0) is the minimum a submission to that
  mode must clear. `0` = raw via `/scores`; `≥1` = a validated run via `/runs`.
- `runs.validation_tier` records what the validator actually achieved (`≥`
  required), and that scalar is denormalized onto `scores.validation_tier` for
  the read path. `validated` is derived as `tier > 0`.
- The `Validator` interface is **tier-agnostic**: `validate(run, required_tier)`
  returns a `ValidationResult(canonical_score, tier_achieved, status, reason)`.
  A `TieredValidator` dispatches:
  - **Tier 1** — bounds/shape only; the plausible claim becomes canonical (no
    recompute, so it is the weakest tier).
  - **Tier 2** — a per-`scenario_version` scorer recomputes the canonical score
    from the action log; the claim is recorded but never trusted, and a mismatch
    is *not* a rejection (the recomputed value wins).
  - **Tier 3** — deterministic replay. Real in the design, **deferred in
    binding**: it exists as a marked integration point that currently rejects.
    Whether the replay core runs in-process, as a subprocess, or as a sidecar is
    an open decision and is not encoded here.

The mode sets the floor; the validator may achieve higher and records it. The
single alternative — a flat "validate or don't" — was rejected because it can't
let a cheap mode stay cheap while a competitive mode demands replay, and it
gives no path to strengthen assurance without schema or endpoint churn.

## Consequences

### Positive

- An upgrade path with no schema churn: raising a mode's `required_tier`, or
  re-validating an old run at a higher tier later, needs no migration — the
  action-log blob is retained and `tier_achieved` is just a number.
- Cost scales with the assurance a mode actually needs; tier-0 modes pay nothing
  for machinery they don't use.
- The `Validator` seam isolates the (deferred) replay-core binding from the rest
  of the system, so endpoints and write semantics were built and tested without
  it.

### Negative

- Tiers 2 and 3 require artifacts that do not exist in-repo yet: a per-scenario
  scorer (tier 2) and the replay core + its binding (tier 3). Consequently a
  mode configured at `required_tier ≥ 2` currently **rejects every submission**
  — an operational caveat (configure only tier 1 in production for now), not a
  bug.
- Tier 1 trusts the claim within bounds, so enabling a tier-1 mode buys the
  `/runs` flow, anti-replay, and a recorded-not-trusted claim, but **not**
  anti-cheat — real recompute resistance is tier 2/3.
- The tier-3 binding (in-process port / subprocess / sidecar) remains an open
  decision; the Heroku polyglot-deploy story for a non-Python core is unverified.

### Neutral

- `required_tier` defaults to 0, so every pre-existing mode is unaffected.
- `validation_tier` is CHECK-constrained to 0–3 on both `runs` and `scores`.
- `validated` / `validation_tier` are surfaced read-only on `ScoreResponse`;
  the read path reads the denormalized `scores.validation_tier` (no join).
