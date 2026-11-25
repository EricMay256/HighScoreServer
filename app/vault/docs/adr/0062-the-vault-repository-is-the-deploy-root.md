# 62. The vault repository is the deploy root, and composes HSS's public `main` at build

Date: 2026-11-25

## Status

Accepted 2026-11-25. The operator answered the proposal's three forks the same day, and
the second answer inverted its direction: the private repository deploys, HSS does not.
The build mechanism still has to be proven on a staging app before cutover.

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
4. **HSS gains a generic extension point and names no extension.** `create_app` accepts
   extensions. Each one provides an `include(app)`, called where the vault block sits
   today, ahead of the SPA catch-all, and a `lifespan(app)` context, entered inside
   HSS's lifespan. The vault package implements one. HSS's own `wsgi.py` passes none,
   so HSS deploys standalone as a leaderboard-only app.
5. **`knowledge-vault` owns the composed deployment:** a `wsgi` module calling
   `create_app(extensions=[vault_extension])`, a Procfile, and a release script running
   HSS's leaderboard lineage and then the vault lineage.
6. **`knowledge-vault`'s CI tests the composition** by checking out HSS's public `main`,
   which needs no credential. It runs the manifest's "private composition" tests there.
   HSS's own CI stays leaderboard-only.

## Consequences

- A push to `knowledge-vault`'s `main` deploys. **An HSS-only change reaches production
  only on the next build**, either a manual rebuild or the next vault deploy. If that
  lag matters, add a workflow later. It needs a Heroku key, held outside the public
  repository.
- Before anything moves, HSS changes in-repo: the extension point lands, and the vault
  wiring in `app/main.py` becomes a vault extension registered behind `VAULT_ENABLED`.
  That is testable while both halves share one suite, and it is the extraction's first
  commit.
- Unverified, and the staging spike must answer it: how the build fetches HSS before the
  Node and Python buildpacks run, whether through a first buildpack or a build hook.
  Check that the SPA build and the leaderboard migrations both find the fetched tree.
- One new private GitHub repository, approved by the operator 2026-11-25. No token
  config var is needed, and no new add-ons.
- Vault development needs local checkouts of both repositories. The composition root
  should find HSS through an environment variable or an editable install, never a
  hardcoded path.
