# Next steps

Current as of 2026-08-21. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF.md` holds the full task list and session
history; `HANDOFF-METADATA.md` holds the metadata-model decision brief. This file
exists so neither has to be read to know what to pick up.

**State:** branch `ai-claude/vault-doc-shell-fixes`, 1 commit ahead of
`origin/dev` and unpushed. `origin/dev` is 5+ commits ahead of `main`.
Suite green (416 vault, 642 full).

**Vault lineage head in the repo is `0011_review_candidate_optional`. Production
is at `0010_document_origin`.** That gap is the review flow, which is merged to
`dev` and deliberately not deployed yet. A database that predates the head needs
`alembic -c alembic-vault.ini upgrade head` before this code runs against it.

**Production is healthy** as of the 2026-08-21 corpus migration: 68 documents,
all active, none flagged, none unembedded, every path a title slug.

---

## 1. Deploy the review flow

`dev` carries migration `0011` and three REST routes the running service does
not have. Nothing is broken by the gap — the routes simply are not there — but
the branch and production disagree about the schema, and that is a state worth
closing rather than carrying.

The deploy is ordinary: the release phase runs the vault lineage, so `0011`
lands with the code. Migration 0011 only drops constraints, so it is fast and
carries no rewrite.

**Before deploying, decide ADR 0023's status.** It is `Proposed`, and it governs
what the exporter is allowed to touch. Deploying code whose governing decision is
still proposed is how a proposal becomes accepted by accident.

## 2. Accept, revise, or reject vault ADR 0023

"The export projects only the engine-managed folders, not all of `Agent/`."
Written 2026-08-19, still `Proposed`.

It matters because it contradicts the compilation plan in
`HANDOFF-EXPORT-AND-COMPILATION.md`, which assumed notes carrying a proposed type
would be *exported into* `Agent/Promotion Candidates/`. The Promotion Policy calls
that folder a human-curated queue kept outside the engine, and `folders.yml` marks
it `engine_managed: false`. The ADR sides with the governance documents. Phase 4
cannot be designed until this is settled.

## 3. Revoke the `importer` credential

`db11bca5fa415f42` (principal `importer`) still holds `vault:read vault:write
vault:update` and has no remaining purpose — the 2026-08-21 re-import ran under
`importer-b`, which has since been revoked. `claude-1` is the working credential.

Not urgent, but it is a live write credential whose job is finished.

## 4. OAuth, if web access matters

Vault ADR 0024 (`Proposed`) settles the design: the vault hosts its own
authorization server, and an issued token is backed by a `vault_agent_credentials`
row rather than a parallel identity. Decide the ADR, then build.

Smaller than it sounds, and that was checked rather than assumed. The MCP SDK
already ships `create_auth_routes()` with `/authorize`, `/token`, `/register`,
`/revoke` and both metadata documents; `principal.resolve_credential` already has
the `TokenVerifier` shape ADR 0015 left for it; `bcrypt` is already a dependency.
The work is one ten-method provider protocol, the operator password, and wiring.

Two ordering constraints, both cheap to honour and expensive to discover late:

- **Verify the mobile flow before building the provider.** Mobile contribution is
  the reason this matters, and Google refuses OAuth in embedded webviews with no
  way to disable it. Register the Google client, wire `authorize` to redirect,
  and try it from the phone. If that client uses an in-app webview, the password
  form is the method that works -- which is why ADR 0024 builds both.
- **Do not ship the `resource_metadata` challenge before the server answers.**
  That header is the vault advertising an authorization server; pointing at one
  that is not there is worse than the current honest dead end.

## 5. Compilation (Phase 4)

The last markdown writer. `Agent/wiki/` is still produced by the Stage-A
librarian loop in the knowledge-platform engine, because the service has no
compile path. `vault_compile_runs` already exists with its provenance CHECK and
`ON DELETE RESTRICT`; the service and routes do not.

Two things to carry into that work:

- The exporter refuses to prune a prefix the corpus does not populate. That guard
  is what stops an export deleting the 15 Stage-A wiki pages, and it stops being
  load-bearing the moment the service holds wiki documents.
- Wiki `SourceIDs` now have to come from service note ids, not filenames.

## 6. The admin MCP surface

The review routes are REST-only on purpose (vault ADR 0019's amendment, ADR 0021's
reasoning): reading a case serves `flagged` content and deciding one publishes or
destroys a note, so those tools stay off the surface injected text can name. The
agreed destination is a **separate admin MCP server**, not the existing mount.

Unstarted, and its own design decision with its own ADR.

---

## Deferred, unchanged

**Deferred decision #1 (whole-vault read permissions) still blocks any
human-layer import.** `folders.yml` governs `ai_write` and has no per-credential
`ai_read`; one `vault:read` scope reads everything the read policy admits.

**`superseded` is a reserved review state** with no decision path that sets it.
Leave it that way until there is a case that needs it and a reason to write down.
