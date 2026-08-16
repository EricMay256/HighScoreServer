# 4. Stage A `core.decide()` is normative for the write path

Date: 2026-07-28

## Status

Accepted

## Context

Contribution policy — what makes a submitted note acceptable, duplicate, near-duplicate, or
rejected — is already implemented and in use in the Stage A engine, `vault_contrib`. Its
`core.decide()` function and `models.Policy` type were deliberately written as a pure core
with no I/O: no database, no network, no filesystem. They have a test suite that encodes the
accumulated edge cases.

The vault package now under construction will eventually own the governed write path. The
tempting move, while building it, is to start expressing policy in `app/vault/` — writing
service-layer rules that look like the Stage A ones — so that the new code path is "complete".

That would create two implementations of the same policy, divergent from the moment the second
one is written, with no mechanism to detect the divergence. Prose descriptions of policy are
lossy: the behaviours that matter are precisely the edge cases that a summary omits, and those
are exactly what the Stage A tests pin down.

The current vault phase is read-only. It has no need for policy logic at all.

## Decision

`vault_contrib.core.decide()` and `vault_contrib.models.Policy` remain the single source of
truth for contribution policy until a deliberate switchover.

`app/vault/` contains **no** policy logic during the read-only phase. Not a port, not a
partial reimplementation, not a "temporary" simplified version.

At switchover, `decide()` and `Policy` are moved into the vault package **verbatim, together
with their test suite**, and the tests must pass unmodified before the new write path is
enabled. They are not reimplemented from prose, from this ADR, or from the architecture
document.

Keeping Stage A's core free of I/O is what makes this possible, and that property is to be
preserved on both sides of the move.

## Consequences

There is exactly one implementation of contribution policy at any time, so the two cannot
drift. The switchover is mechanical and its correctness is checkable — the existing tests
either pass against the moved code or they do not.

Until switchover, the vault package cannot serve governed writes. This is intended: the
current phase is read-only, and a write path that bypassed `decide()` would be worse than no
write path.

The port is gated on `decide()` remaining I/O-free. If Stage A evolution introduces database
or network calls into the pure core, this decision needs revisiting, because the verbatim move
would then drag a dependency graph with it. Reviewers of `vault_contrib` should treat new
imports in the core module as a signal to revisit this ADR.
