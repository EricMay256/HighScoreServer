# 62. The extracted runtime is composed into HSS at build, tracking both mains

Date: 2026-11-25

## Status

**Proposed** 2026-11-25. Three forks below need the operator's answer before
execution. Partially supersedes "Eventual repository and CI composition" in
`vault-architecture.md`: the component lock and composition in the private
repository are replaced. Its leak checks are kept.

## Context

Extraction was deferred against a consumer trigger. It is now scheduled, with an
operational definition of done: **both products are hosted on the one Heroku app as
an ongoing arrangement, and a Heroku rebuild deploys the current head of `main` from
both repositories.**

What constrains the shape:

- HSS deploys with the Python and Node buildpacks and is not a pip package. Its SPA is
  built by `package.json`'s `heroku-postbuild`, and `app/main.py` composes everything.
- knowledge-platform holds the corpus (`Vault/`). The architecture document requires
  that corpus content never reach a build artifact. Deploying from that repository
  would put the corpus into Heroku's build input.
- The public repository must never hold a credential that can read a private one.

## Decision (proposed)

1. **The runtime moves to a new private repository, holding no corpus.** It contains
   `app/vault/` as an installable package with its own import name, `vault_migrations/`,
   the vault-owned scripts and tests from the manifest, its docs and ADRs.
   knowledge-platform keeps corpus, governance, skill and plugin.
2. **HSS remains the Heroku deploy root and composition point, through one seam.** When
   `VAULT_ENABLED` is true, `app/main.py` imports the package's public `mount(app)` and
   lifespan and nothing else. The package never imports HSS. HSS's base requirements
   and CI do not include it, so the public CI runs leaderboard-only.
3. **At build, HSS installs the runtime from the private repository's `main`**, using a
   read-only token held only as a Heroku config var. The token is fine-grained, with
   contents:read on that one repository. The release phase runs the leaderboard lineage,
   then the package's migration entry point, when `VAULT_ENABLED`. The build logs both
   resolved commit SHAs.
4. **The private repository's CI tests the composition** by checking out HSS's public
   `main`, which needs no credential. It runs the manifest's "private composition"
   tests against it.

## Forks for the operator

- **Head of `main` versus a pinned lock.** Tracking heads is the definition of done.
  The cost is that a rebuild is not reproducible: two rebuilds can ship different
  runtime revisions. Logging the SHAs makes that visible, not reversible.
- **What triggers a deploy when only the runtime changes.** A push to the private
  `main` does not rebuild HSS. The options are a manual rebuild, or a workflow in the
  private repository that calls Heroku's build API with a key held there.
- **Name and import name of the new repository and package.** For example
  `knowledge-vault` and `knowledge_vault`.

## Consequences

- HSS keeps a few lines naming an optional package. That is a smaller dependency than
  today's whole directory, and the reverse boundary test stays in HSS.
- Unverified: whether the Python buildpack re-resolves an unpinned `git+…@main`
  requirement on every build or reuses its cache. Test it on a staging app first, and
  force a reinstall in the build step if needed.
- Vault development needs a local checkout of both repositories; an editable install
  covers it.
- **Flag: this is a new GitHub repository and a new Heroku config var**, both
  requiring approval under the host's no-new-infrastructure rule. No new add-ons are
  involved.
