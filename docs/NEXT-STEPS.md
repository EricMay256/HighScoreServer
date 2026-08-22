# Next steps

Current as of 2026-08-22. A short, ordered list — the *why now* and the blocking
relationships, not the detail. `HANDOFF.md` holds the full task list and session
history; `HANDOFF-METADATA.md` holds the metadata-model decision brief. This file
exists so neither has to be read to know what to pick up.

**State:** on `dev`, pushed, **13 commits ahead of `main`**. Suite green (416
vault, 642 full).

**Production is current with `dev`'s schema:** vault lineage
`0011_review_candidate_optional`, 70 documents, all active, none flagged, none
unembedded, every path a title slug. The review flow shipped in release v64.

**Two ADRs are `Proposed`, and both gate work below:** 0023 (what the export may
touch) and 0024 (the OAuth authorization server).

---

## 1. Merge `dev` into `main`

Thirteen commits, including every piece of vault documentation written this month.
This is not bookkeeping: someone looking for the MCP setup instructions could not
find them, because they were on `dev` and GitHub shows `main`. Documentation on a
non-default branch is documentation nobody reads.

Nothing on Heroku tracks `main`, so this changes no running behaviour.

## 2. Settle ADR 0023

"The export projects only the engine-managed folders, not all of `Agent/`."

It contradicts the compilation plan in `HANDOFF-EXPORT-AND-COMPILATION.md`, which
assumed proposed-type notes would be *exported into* `Agent/Promotion Candidates/`.
The Promotion Policy calls that folder a human-curated queue kept outside the
engine, and `folders.yml` marks it `engine_managed: false`; the ADR sides with the
governance documents. **Phase 4 cannot be designed until this is decided**, and
the exporter already behaves as the ADR describes — so leaving it `Proposed` means
shipped code whose governing decision is unsettled.

## 3. Settle ADR 0024, then build the authorization server

"The vault runs its own authorization server, and an OAuth token is a credential
row." The design is settled in the ADR; what remains is accepting it and writing
the provider.

The 2026-08-22 spike answered the question that blocked it — `/authorize` runs in
the operator's system browser, so Google login is viable — and left three
constraints the implementation must honour:

- **Client registrations persist to Postgres, never process memory.** Registration
  is server-to-server from the vendor's backend while authorization is a browser
  navigation, so the two halves reliably land on different workers. The spike's
  first version used a dict and failed exactly there, deterministically.
- **The `401` needs no `resource_metadata` parameter.** Clients construct the
  well-known URL by convention, so `mcp.py` does not have to change.
- **The SDK's routes break handler introspection.** slowapi reads
  `handler.__name__`; the SDK wraps some endpoints in `CORSMiddleware`, which has
  none. Label them, or the first request raises inside the rate limiter.

**A `grant`/`revoke-scope` subcommand on `issue_vault_credential` must land with
it.** OAuth clients all start at the read+write baseline and some will need more;
the only documented way to widen is a raw `UPDATE` on `scopes`, which does not
survive becoming routine.

`app/vault/oauth_spike.py` has served its purpose and should be deleted in the
same commit that adds the real provider — keep it until then, since its route
wiring and slowapi workaround are both reused.

## 4. Revoke the `importer` credential

`db11bca5fa415f42` (principal `importer`) still holds `vault:read vault:write
vault:update` and has no remaining purpose: the 2026-08-21 re-import ran under
`importer-b`, which is revoked. `claude-1` is the working credential.

A live write credential whose job is finished.

## 5. Compilation (Phase 4)

The last markdown writer. `Agent/wiki/` is still produced by the Stage-A librarian
loop in the knowledge-platform engine, because the service has no compile path.
`vault_compile_runs` already exists with its provenance CHECK and
`ON DELETE RESTRICT`; the service and routes do not.

Two things to carry in:

- The exporter refuses to prune a prefix the corpus does not populate. That guard
  is what stops an export deleting the 15 Stage-A wiki pages, and it stops being
  load-bearing the moment the service holds wiki documents.
- Wiki `SourceIDs` now come from service note ids, not filenames.

Blocked on ADR 0023.

## 6. The admin MCP surface

The review routes are REST-only on purpose (ADR 0019's amendment, ADR 0021's
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

**The knowledge-vault skill's compile loop is its last Stage-A writer.** Note
contribution moved to the service; wiki compilation did not, and cannot until
item 5 lands.
