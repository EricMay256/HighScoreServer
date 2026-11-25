# 62. The vault repository is the deploy root, and composes HSS's public `main` at build

Date: 2026-11-25

## Status

Accepted 2026-11-25. The operator answered the proposal's three forks the same day, and
the second answer inverted its direction: the private repository deploys, HSS does not.
The operator also chose to skip a staging spike. The build mechanism is proven at
cutover, under the rollback plan below.

Amended 2026-11-25: decisions 4 and 5 now name two applications behind a path dispatcher
(host ADR 0016) instead of an extension point inside HSS.

Partially supersedes "Eventual repository and CI composition" in
`vault-architecture.md`. That section's direction is kept — the private side composes,
and neither product imports the other's domain. Its component lock is replaced by
tracking `main`. Its leak checks still apply.

## Context

Extraction was deferred against a consumer trigger. It is now scheduled, with an
operational definition of done: **both products are hosted on the one Heroku app as an
ongoing arrangement, and a Heroku rebuild deploys the current head of `main` from both
repositories.**

Constraints:

- HSS deploys with the Python and Node buildpacks and is not a pip package. Its SPA is
  built by `package.json`'s `heroku-postbuild`. `app/main.py` composes everything, with
  the SPA catch-all registered last, so anything added after `create_app()` returns
  would sit behind it.
- knowledge-platform holds the corpus (`Vault/`), which must never reach a build.
- The public repository must never hold a credential that can read a private one.

## Decision

1. **A new private repository, `knowledge-vault`, holds the runtime and no corpus.** It
   contains the package (import name `knowledge_vault`), `vault_migrations/`, the
   vault-owned scripts and tests listed in the manifest, docs and ADRs, and the
   composition root. knowledge-platform keeps corpus, governance, skill and plugin.
2. **The Heroku app `high-score-server` is re-pointed to `knowledge-vault`.** Its
   add-ons, database and config stay. Heroku's GitHub integration deploys that
   repository's `main`.
3. **At build, `knowledge-vault` fetches HSS's public `main`.** That needs no
   credential. The SPA is built from it and both sets of requirements are installed.
   The build logs both resolved commit SHAs. Tracking heads rather than a lock is the
   operator's choice: a rebuild is visible but not reproducible.
4. **HSS and the vault are two independent applications** (host ADR 0016, amended in
   here 2026-11-25). HSS's `create_app()` names no vault code; `create_vault_app()`
   builds the vault alone; `ComposedApp` sends a request to the vault when one of its
   routes matches the path, and runs both lifespans. HSS's own `wsgi.py` serves the
   leaderboard alone after extraction.
5. **`knowledge-vault` owns the composed deployment:** `composed.py` with its import
   path changed, a `wsgi` module, a Procfile, and a release script running HSS's
   leaderboard lineage and then the vault lineage.
6. **`knowledge-vault`'s CI tests the composition** by checking out HSS's public `main`,
   which needs no credential. It runs the manifest's "private composition" tests there.
   HSS's own CI stays leaderboard-only.

## Consequences

- A push to `knowledge-vault`'s `main` deploys. **An HSS-only change reaches production
  only on the next build**, either a manual rebuild or the next vault deploy. If that
  lag matters, add a workflow later. It needs a Heroku key, held outside the public
  repository.
- Before anything moves, HSS changes in-repo: `create_vault_app()` and `composed.py`
  land and `app/main.py` drops the vault (host ADR 0016). That is the extraction's
  first commit, testable while both halves share one suite.
- Unverified until cutover: how the build fetches HSS before the Node and Python
  buildpacks run, whether through a first buildpack or a build hook, and whether the SPA
  build and the leaderboard migrations both find the fetched tree.
- **Rollback plan, since there is no spike:**
  - Cut over only when production already runs HSS `main` with every migration from
    both lineages applied. The first `knowledge-vault` release phase is then a no-op
    `upgrade head`, not a schema change.
  - Keep the vault inside HSS until the cutover is verified. Re-pointing the app back
    to HSS then restores the previous arrangement exactly.
  - `heroku rollback` returns to the previous release's slug immediately, whatever
    repository built it.
  - Remove the vault from HSS only after the composed app has served real traffic.
- One new private GitHub repository, approved by the operator 2026-11-25. No token
  config var is needed, and no new add-ons.
- Vault development needs local checkouts of both repositories. The composition root
  should find HSS through an environment variable or an editable install, never a
  hardcoded path.
