# Vault configuration and Heroku operations

**Status:** Phase 1 persistence-foundation runbook

**Initial topology:** one Essential-0 PostgreSQL database, `public` and `vault`
schemas

**Contains secrets:** no

**Nothing in this document has been applied. Re-verified on 2026-08-16:
`VAULT_ENABLED` is unset on `high-score-server`, and a read-only catalog query
returns NULL for `to_regclass('vault.vault_agent_credentials')`, so the vault
credential schema has never been deployed.**
Read every command here as a plan, not as a description of the running app. The
vault ships dark by design — `VAULT_ENABLED` defaults to false, so no routes are
registered, no engine is created, and `scripts/release.sh` skips the vault
migration lineage — which is why the code can merge to `main` well before any of
this is configured. Confirm the real state with `heroku config --app
high-score-server` before acting on anything below.

This document records variable names and operator commands only. Never paste
real database URLs, tokens, note content, exports, or embedding vectors into
this file, source control, CI logs, or build artifacts.

## Phase 1 behavior

The persistence foundation is feature-gated. `VAULT_ENABLED=false` leaves the
vault engine closed and preserves the existing HSS request path. Phase 1 does
not add routes, credentials, embedding providers, import jobs, or deployment
changes.

The production Essential-0 plan permits 20 connections. With two Gunicorn
workers, the approved initial allocation is:

```text
(HSS pool max 4 + vault pool size 2) * 2 workers
  + 2 release/operator connections
  = 14 allocated

20 total - 14 allocated = 6 unallocated (30%)
```

The 30% remainder satisfies the architecture's requirement to leave at least
25% unallocated.

**Production evidence, 2026-08-16 (vault still disabled):** Heroku explicitly
sets `HSS_DB_POOL_MAX_SIZE=4`; one Basic web dyno runs the Procfile's two Gunicorn
workers; Essential-0 reported 2/20 live connections. Across the 52 router samples
available in the recent log window, service time was p50 47 ms, p95 1,451 ms,
max 4,789 ms, with zero pool-timeout signals, H12s, or router 5xx responses.
This supports retaining 4 for the current low-traffic disabled state; it is not a
substitute for a load test. `HSS_PROCESS_COUNT` and `VAULT_DB_POOL_SIZE` remain
unset in production. Set them explicitly to 2 in the reviewed vault-enablement
release and repeat the connection/latency check after traffic reaches the vault.

**The split moved from 5/1 to 4/2 on 2026-08-14, at the same total.** A vault
request checks out twice in sequence — once to authenticate, once to serve — so
at pool size 1 a second concurrent request waited out `pool_timeout` and failed:
the surface could not serve two callers at once. HSS gave up its fifth
connection to pay for it, because 5 was never measured (it is the original
default in `app/db.py`) whereas the vault needing two is measured in its own
timeout. `validate_connection_budget` passes by exactly one connection either
way, so **both variables must move together** — setting only
`VAULT_DB_POOL_SIZE=2` gives 16 against an available 15 and raises `RuntimeError`
at lifespan, taking the leaderboard down with the vault.

Two consumers this formula does not count. The release dyno takes one connection
for `alembic upgrade head` while the web dynos are still up (14 + 1 = 15 — inside
the hard limit, but spending the reserve). And preboot doubles every per-worker
figure, so `heroku features:enable preboot` needs the pool sizes halved first or
new dynos fail to boot mid-deploy.

## Non-secret Heroku configuration

Do not apply these settings as part of Phase 1 development. Apply them in the
reviewed release that first enables the vault runtime:

```bash
heroku config:set \
  HSS_DB_POOL_MIN_SIZE=1 \
  HSS_DB_POOL_MAX_SIZE=4 \
  HSS_PROCESS_COUNT=2 \
  DATABASE_CONNECTION_LIMIT=20 \
  DB_OPERATIONAL_CONNECTION_RESERVE=2 \
  VAULT_DB_POOL_SIZE=2 \
  VAULT_DB_POOL_TIMEOUT_SECONDS=5 \
  VAULT_EMBEDDING_TIMEOUT_SECONDS=5 \
  VAULT_TEXT_SEARCH_CONFIG=english \
  VAULT_ENABLED=true \
  --app high-score-server
```

In PowerShell the line-continuation character is a backtick (`` ` ``) rather
than `\`; the arguments are otherwise identical.

`VAULT_EMBEDDING_TIMEOUT_SECONDS` is listed here, not only with the other
embedding settings below, because it is **per attempt** and a plausible-looking
value silently exceeds Heroku's router budget. Read
"[`VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt, not per
request](#vault_embedding_timeout_seconds-is-per-attempt-not-per-request)"
before changing it. If it is already set on the app to anything above 7.3, this
release will refuse to boot until it is corrected — check first:

```bash
heroku config:get VAULT_EMBEDDING_TIMEOUT_SECONDS --app high-score-server
```

The `Procfile` currently fixes Gunicorn at two workers. If that count changes,
update `HSS_PROCESS_COUNT` in the same release and recalculate the budget before
deploying.

**Do not replace `-w 2` with `-w ${WEB_CONCURRENCY}` to remove that coupling.**
Heroku's Python buildpack sets `WEB_CONCURRENCY` in the dyno environment at boot
as `min(cores * 2 + 1, RAM_MB / 256)`, and it never appears in `heroku config`
because it is not a config var. Wiring it would make the connection budget a
function of dyno size: a 1 GB dyno yields 4 workers and 26 allocated against a
ceiling of 15, so resizing the dyno would stop the app booting. The Node.js
buildpack writes the same `.profile.d` filename, so with multiple buildpacks the
value would also depend on buildpack order, which this repo does not pin.
Gunicorn's own default for `workers` *is* `WEB_CONCURRENCY`, which is precisely
why `-w` is passed explicitly.

## Text search configuration

```
# Baked into the generated search_vector column at migration time.
# Changing this after migration requires a table rewrite, not a restart.
VAULT_TEXT_SEARCH_CONFIG=english
```

`search_vector` is a **persisted generated column**. Its expression must be
`IMMUTABLE` and is compiled into DDL when the migration runs, so this variable is
read at migration time, not at runtime. An environment change cannot retune the
language of an existing corpus — that needs a table rewrite plus a GIN reindex.

The migration validates the value twice before it reaches DDL: against
`^[a-z_][a-z0-9_]*$`, and against `pg_catalog.pg_ts_config` in the target
database. A name PostgreSQL does not recognise aborts the migration rather than
silently producing a schema nobody asked for.

At startup the vault reads the expression actually stored in the catalog
(`pg_get_expr` over `pg_attrdef`, for the column where `attgenerated = 's'`) and
refuses to boot if it disagrees with the environment. Without that check a
mismatch would be silent: queries parsed with one configuration, stored vectors
and their index built with another.

Set this in the same release that first runs `alembic -c alembic-vault.ini
upgrade head`. Changing it later is a migration, not a config change. CI pins it
explicitly so the schema-drift test compares against a fixed target.

Query paths must use the same configuration —
`websearch_to_tsquery(:config, :query)` — rather than relying on the database's
`default_text_search_config`. The lexical arm then disjoins that query's terms
before matching, so a long query does not require every term to be present; see
[ADR 0007](adr/0007-lexical-arm-disjoins-query-terms.md).

## Shared-database URL

The initial topology uses the existing Heroku-managed `DATABASE_URL`. Leave
`VAULT_DATABASE_URL` and `VAULT_DATABASE_CONNECTION_LIMIT` unset:

```bash
heroku config:unset \
  VAULT_DATABASE_URL \
  VAULT_DATABASE_CONNECTION_LIMIT \
  --app high-score-server
```

The vault engine and vault Alembic environment then fall back to
`DATABASE_URL`. Do not copy the value returned by `heroku config:get
DATABASE_URL` into a tracked file.

## Secrets

Phase 1 introduced no new application secret. The read-only slice introduces
two, both listed below.

- `DATABASE_URL` already contains a credential and remains managed by Heroku.
- `API_KEY` and `JWT_SECRET` remain existing HSS secrets.
- `VAULT_EMBEDDING_API_KEY` is introduced by the read-only slice. Store it only
  in Heroku config, never in a tracked file, and never echo it in CI logs. It is
  not logged by the application: the embedding adapter logs status codes only,
  and request bodies are never logged because they carry note content.
- Read access needs **no** environment secret. Agent credentials live in
  `vault.vault_agent_credentials` and are issued with
  `scripts/issue_vault_credential.py`; only the SHA-256 of each secret is
  stored. See vault ADR 0015.
- `VAULT_OPERATOR_PASSWORD_HASH` is introduced by the OAuth authorization
  server (vault ADR 0024). It holds a **bcrypt hash**, never the password, and
  it is the one human credential the vault has. See below.
- Never use `heroku config` output in CI logs or documentation.

Local secrets belong in `.env`, which is already ignored by Git. `.env.example`
contains placeholders and non-secret defaults only.

### `VAULT_OPERATOR_PASSWORD_HASH`

The secret the OAuth login page verifies against, when a client authorizes
itself against the vault.

**It holds a bcrypt hash, never the password.** The value looks like this — a
bcrypt modular-crypt string, ASCII, 60 characters, `$`-delimited:

```
$2b$12$H8choNzlGRYACnlz.LgT1O3sn7Kt7PwtaP7sJKAnIqamtAS/LCeFi
 │  │  └── 22-char salt + 31-char digest, bcrypt's base64 alphabet
 │  └───── cost factor (work exponent), from bcrypt.gensalt()'s default
 └──────── algorithm identifier
```

There is no separate encoding step and nothing to base64 by hand: the string
above *is* the config value, verbatim. Setting the plaintext password here does
not work — it would be compared as though it were a hash and fail every login.

Configuration rather than a table, deliberately: there is exactly one, it has no
lifecycle a schema would model, rotation is `heroku config:set`, and a config
var's backups circulate less widely than a database's do.

**Rotating it is cheap and safe, so rotate on any doubt.** Generate a new hash
and set it; that is the whole procedure. Nothing already issued depends on the
password — it gates future consent approvals only, so live credentials, refresh
families and MCP sessions all keep working. The cost is one release and one
re-approval the next time a client authorizes.

Unset is a supported state and means the password identity method is not
configured for this deployment. It is never treated as "any password works" —
the login refuses outright, the same way `VAULT_ENABLED` defaulting to false
serves no vault rather than an unguarded one.

#### Generating it

```bash
python -m scripts.hash_vault_operator_password
```

It prompts twice without echo, prints the hash, and touches nothing — no
database, no `DATABASE_URL`, no writes. Copy the printed line.

Use the script rather than an inline `python -c`. One that reads with `input()`
prints the password to the terminal; one that takes it as an argument leaves it
in shell history and in the process table. `getpass` avoids both, and on Windows
it reads the console directly — which also means the script cannot be driven by
piping into it, on purpose.

> **PowerShell will happily echo this secret if a command mis-parses. Observed
> 2026-08-24, with the hash on the clipboard.**
>
> `curl` in PowerShell is an alias for `Invoke-WebRequest`, which shares none of
> curl's flags. A command like `curl -s https://host/path` does not fail
> cleanly: the flag binds somewhere unintended, `Uri` ends up unbound, and
> PowerShell **prompts for it** —
>
> ```
> cmdlet Invoke-WebRequest at command pipeline position 1
> Supply values for the following parameters:
> Uri:
> ```
>
> A prompt looks like an invitation to paste, and whatever is pasted is
> **echoed in the clear** and stays in the scrollback. A bcrypt hash pasted
> there is a hash rather than a password, so it is not directly usable — but it
> is the credential guarding the consent screen, and it should not be on screen.
>
> Two habits avoid it. Use **`curl.exe`**, never bare `curl`, so the flags mean
> what they say. And **never answer an unexpected parameter prompt** — press
> `Ctrl+C`, read the command again, and retype it. The prompt means PowerShell
> did not understand what you typed, so nothing good follows from feeding it a
> secret.
>
> The good news, if it happens: PSReadLine records submitted *command lines*,
> not parameter-prompt responses, so the value does not reach
> `ConsoleHost_history.txt`. Verify with
> `Select-String -Path (Get-PSReadLineOption).HistorySavePath -SimpleMatch '$2b$'`
> and expect no matches. Clear the scrollback or close the terminal regardless.

#### Setting it

```bash
heroku config:set VAULT_OPERATOR_PASSWORD_HASH='$2b$12$H8choNzlGRYACnlz.LgT1O3sn7Kt7PwtaP7sJKAnIqamtAS/LCeFi' --app high-score-server
```

**Single-quote the value.** A bcrypt hash contains `$` characters that bash and
`zsh` expand as variables — unquoted, `$2b$12$...` becomes a mangled fragment,
and the symptom is a login that rejects the correct password rather than any
error naming the config var. PowerShell does not expand `$` inside single
quotes either, so the same form is correct there.

Locally the same value goes in `.env`, unquoted, since `.env` is read by the
application rather than by a shell.

#### Three things that go wrong

- **The plaintext password was set instead of the hash.** It will not verify.
  The value must start `$2`.
- **bcrypt truncates at 72 bytes**, so `passwords.py` refuses a longer password
  outright rather than hashing a prefix that would let a different passphrase
  verify. The limit is *bytes*: a passphrase of accented characters reaches it
  sooner than its length suggests, and the script says so rather than silently
  shortening.
- **A malformed hash reads as a wrong password.** The application logs
  `vault operator password hash is not a valid bcrypt hash` at ERROR and reports
  an ordinary login failure — one message for every failure is ADR 0024's rule,
  so the log is where the real cause is findable. If a correct password is being
  rejected, look there before assuming the password is wrong.

### Turning the OAuth authorization server on

The vault hosts its own OAuth 2.1 authorization server (vault ADR 0024) so that
clients with no way to send a static header — the claude.ai web connector — can
still reach it. It is off until `VAULT_PUBLIC_URL` is set, and that variable is
the switch on purpose: every URL in the discovery metadata is absolute, so a
deployment that cannot state its own origin cannot publish correct metadata.
Forgetting it therefore serves nothing rather than something wrong.

```bash
heroku config:set VAULT_PUBLIC_URL=https://high-score-server-xxxx.herokuapp.com --app high-score-server
```

No trailing slash; one is stripped if present. It must be the origin clients
actually reach, because the SDK builds `/authorize` and `/token` from it and a
mismatch surfaces as a client giving up during discovery rather than as an
error.

Two variables together make the flow work, and both are needed:

| Variable | Required | Effect |
| -------- | -------- | ------ |
| `VAULT_PUBLIC_URL` | yes | Publishes discovery metadata and registers `/authorize`, `/token`, `/register`, `/revoke`, `/vault/login`. Absent, none of them exist. |
| `VAULT_OPERATOR_PASSWORD_HASH` | yes, for the password method | What the login page verifies against. Absent, every login refuses. |
| `VAULT_LOGIN_RATE_LIMIT` | no (`10/minute`) | The login POST's own bucket, tighter than the pre-auth guard. |
| `VAULT_REGISTRATION_RATE_LIMIT` | no (`10/minute`) | `/register`'s own bucket. Registration is public, unauthenticated, and writes a row; one client registers once. Defence in depth, not the storage bound — pruning is that. |

Once both are set, a client registers itself and the flow is:

```
POST /register              the vendor's backend, server to server
GET  /authorize             the operator's browser, a real top-level navigation
  -> 302 /vault/login       consent and password on one screen
POST /vault/login           bcrypt verify, mint an authorization code
  -> 303 back to the client with code and state
POST /token                 code + PKCE verifier -> access token + refresh token
```

The access token it issues is an ordinary `hssv1_` credential, so it appears in
`issue_vault_credential list` beside every other one and is revoked the same way.
Its principal is `oauth-<client_id>` — the server-issued registration id, never the
client's self-declared name — and that is also what lands in `ContributedBy` on notes
the client writes. The readable name is on the credential's `display_name`, which is
what `issue_vault_credential list` shows. A name-derived principal collided across
separately registered clients that chose the same name, which meant sharing an
idempotency namespace and a quota; see vault ADR 0024's 2026-08-23 amendment.

**Scopes are capped at `vault:read`, `vault:write`, and `vault:propose`.** A client cannot request
more — `vault:update`, `vault:delete` and `vault:review` are unreachable through
this path by construction, not by an operator declining on a screen. That is a
security decision: ADR 0021's defence against instructions injected into note
text is that a destructive tool is absent from the surface that text can name.
`vault:propose` stores an inert, revision-bound suggestion; it cannot apply it.

**Access tokens live one hour; refresh tokens thirty days.** The client renews
itself, so the operator authorizes roughly monthly rather than hourly. Each
refresh mints a new credential row and revokes the previous one, so revoked rows
accumulate — expected, and they grant nothing.

**A replayed refresh token revokes the whole chain.** If a rotated token is
presented again, every credential ever minted from that authorization is
revoked. The legitimate client simply re-authorizes; the symptom an operator
sees is a connector asking to be reconnected, and the cause is in the log as
`vault oauth refresh token replayed; family revoked`.

#### Troubleshooting the flow

| Symptom | Cause |
| ------- | ----- |
| Client reports the server does not support OAuth | `VAULT_PUBLIC_URL` unset, so no metadata is published |
| `/authorize` 302s to a login page that says the request is no longer valid | The nonce expired (5 minutes) or was already used |
| Correct password rejected every time | `VAULT_OPERATOR_PASSWORD_HASH` unset or mangled — check the log for `not a valid bcrypt hash` |
| Login returns 429 | The login bucket; wait a minute, or raise `VAULT_LOGIN_RATE_LIMIT` |
| `/register` returns 429 | The registration bucket; wait a minute, or raise `VAULT_REGISTRATION_RATE_LIMIT`. Re-registering repeatedly is itself unusual — a client registers once. |
| A typo'd password needs restarting from the client | Deliberate: a submit redeems the nonce whether or not the password was right, so a wrong guess burns that authorization |

#### Why bcrypt here and SHA-256 everywhere else

Agent secrets (`hssv1_…`) are machine-generated with full entropy, so a plain
digest is correct for them: there is no dictionary a work factor would slow
down, and the read surface cannot afford a deliberately slow hash per request.
An operator password is chosen by a person, so the work factor is exactly the
point, and it runs once per authorization rather than once per request. Vault
ADR 0015 says explicitly not to carry the SHA-256 reasoning across to
human-chosen passwords; this is where that matters.

### The server tells you when `VAULT_PUBLIC_URL` goes stale

`VAULT_PUBLIC_URL` is configuration rather than something derived per request,
for three reasons: the OAuth routes are built at application assembly, before
any request exists; deriving an issuer from the `Host` header would let a forged
header point `token_endpoint` at somebody else's server; and its absence is the
feature's off switch.

What configuration cannot do is notice that it has gone stale — after a custom
domain, a proxy, or a renamed app. A stale value publishes discovery documents
pointing somewhere wrong, and the symptom surfaces at the *client* as "this
server does not support OAuth", a long way from the cause.

So the first OAuth request of each process compares the configured origin with
the one the request arrived on and logs the result once:

```
INFO  vault oauth public url matches the host requests arrive on
WARN  vault oauth public url does not match the host requests arrive on;
      discovery metadata advertises https://… while requests arrive on https://…
```

The observed value is **only** ever logged. It is never used to build a URL —
reading `Host` to write a log line is safe in a way that reading it to publish
an issuer is not. On a warning, the configured value is still what clients are
sent to, so the fix is to update `VAULT_PUBLIC_URL`, not to trust the header.

A trailing slash is not drift: `main.py` strips it and the check agrees.

## Migration lineages

The leaderboard lineage owns `public.*` and records its revision in
`public.alembic_version`:

```bash
alembic upgrade head
```

The vault lineage owns `vault.*` and records its revision in
`vault.vault_alembic_version`:

```bash
alembic -c alembic-vault.ini upgrade head
```

The Heroku release phase is `bash scripts/release.sh`. It runs the leaderboard
lineage unconditionally, then the vault lineage **only when `VAULT_ENABLED` is
enabled**, and aborts the release if either fails. Release and runtime use the
same case-insensitive boolean contract: `1`, `true`, `yes`, and `on` enable;
`0`, `false`, `no`, and `off` disable; surrounding whitespace is ignored. An
unset value defaults to `false`, while an empty or unrecognized value aborts the
release instead of silently skipping migrations. Use lowercase `true` and
`false` in operator configuration for clarity.

The gate is deliberate. `0001_vault_foundation` runs `CREATE EXTENSION vector`;
if pgvector is unavailable on the attached plan, an ungated release phase would
abort *every* deploy, including ones unrelated to the vault. Because setting
`VAULT_ENABLED=true` itself triggers a release, the cutover is exactly when the
vault schema is built and a failure aborts that release rather than an
unrelated one. **Verify pgvector on the target plan before flipping the flag:**

```bash
heroku pg:psql --app <app> -c   "SELECT name, installed_version FROM pg_available_extensions WHERE name='vector';"
```

Vault migrations may enable the database-wide `vector` extension, but they
never import Markdown, generate embeddings, or read the private
knowledge-platform repository.

Migration `0016_amendment_proposals` adds `content_revision`, the durable amendment workflow,
and the `vault:propose` scope vocabulary. Proposal rows are adjudication history rather than
disposable queue entries. Its downgrade therefore **refuses while any proposal row exists**;
removing that history must be a separate, deliberate data decision, never an incidental
rollback step. Production recovery remains a forward application release—do not downgrade
0016 merely to run an older slug.

## Pre-deployment verification

After authenticating the Heroku CLI:

```bash
heroku pg:info --app high-score-server
heroku pg:psql --app high-score-server
```

Run these read-only SQL checks in `psql`:

```sql
SELECT current_setting('max_connections');

SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name = 'vector';

SELECT version_num FROM public.alembic_version;
SELECT version_num FROM vault.vault_alembic_version;
```

Expected production facts:

- plan: Essential-0;
- connection limit: 20;
- `vector` is available;
- the two version tables contain heads from different Alembic lineages.

## Corpus migration: replacing the imported notes

Written from the 2026-08-21 run that carried `origin` and slug paths into production. Follow
it whenever the corpus has to be re-imported — the shape is the same even when the reason is
not.

**The one thing that governs everything else: find out what is native first.** Notes
contributed directly through the live service exist *only* in the database. A re-import
cannot recreate them, and an unfiltered wipe destroys them. The August run found one, and it
was a good note. Census before you plan, not after:

```sql
SELECT contributed_by, count(*) FROM vault.vault_documents GROUP BY 1 ORDER BY 2 DESC;
```

### Order, and why it is not negotiable

1. **Census** (read-only). Counts by contributor, `vault_path` shapes, review cases,
   credentials. Nothing is decided until this comes back.
2. **Back up, *after* the deploy.** A backup taken before the release captures the
   pre-migration schema, so restoring it leaves the database a revision behind the running
   code and needs `alembic -c alembic-vault.ini upgrade head` before the app works again.
   The data is safe either way; the rollback is only one step if the backup is post-deploy.
   `heroku pg:backups:capture --app <app>`.
3. **Deploy.** The release phase runs the vault lineage. **This must precede the import**:
   `VaultContributionRequest` sets `extra="forbid"`, so an import sending a field the
   deployed model does not know is a 422 on every note.
4. **Verify the deploy in two places.** The migration (`vault.vault_alembic_version`, plus
   the columns and constraints it added) *and* the running code — fetch `/openapi.json` and
   confirm the request model carries the new field. A migration that landed while the dynos
   still run old code looks fine from the database side.
5. **Export the corpus as it stands.** A readable snapshot on disk, before anything
   destructive, including the native notes. Validate it: `python -m vault_governance.cli
   validate --vault <snapshot-root>` with a copy of `00 Governance/` beside the `Agent/`
   tree.
6. **Issue a fresh import principal.** Not the previous one. The ledger is keyed
   `(principal_id, idempotency_key)` and the import re-sends the same keys, so reusing the
   old principal makes every note either a silent replay or — once the request body has
   changed, which is usually the point — a `409` conflict on all of them. A new principal
   sidesteps both and leaves the old ledger intact for ADR 0019's reasons.
7. **Dry-run the importer before wiping.** It reports exactly what each note would send and
   what, if anything, is dropped. Doing this after the wipe means discovering a payload bug
   with an empty corpus.
8. **Wipe, filtered to the import principal.** Delete through
   `VaultDocumentRepository.delete`, never raw SQL: it clears the write-request and
   review-case pointers so the ledger and any judgements survive with a null reference. Raw
   `DELETE` either trips the foreign keys or takes the audit trail with it.
9. **Import.** ~2s per note is the built-in pacing and matches the sustained contribute
   quota, so a 60-note corpus takes about two minutes. Pass `--map` so the
   original-id → new-id mapping does not live only in scrollback.
10. **Verify.** Counts, provenance coverage, path shapes, embeddings, no orphan documents.
    Export again and re-run the governance validator; a second export immediately after
    should report every file unchanged, which is what proves the projection is stable.
11. **Revoke the import credential**, and any other write credential that has outlived its
    purpose.

### If the import fails partway

**Do not re-wipe.** The importer is idempotent per `(principal, key)`, so re-running it
resumes and skips what already landed. Wiping again throws away the partial progress and
starts the clock over.

### Credential tokens are printed once, to stdout

`issue_vault_credential` prints the token and cannot recover it. When an agent or a shared
session runs the command, that token lands in a transcript — which has already happened once
to a still-live production credential. Prefer issuing the credential yourself in your own
shell, or redirect the output to a file you control. Revoke anything that leaks:

```bash
heroku run --app <app> "python -m scripts.issue_vault_credential revoke --id <credential-id>"
```

### `heroku run` eats flags meant for your script

`heroku run -a <app> python -m scripts.x --id abc` fails with `Nonexistent flags: -m, --id`:
Heroku's own parser claims them before the remote command sees them. Quote the whole remote
command as a single argument:

```bash
heroku run --app <app> "python -m scripts.issue_vault_credential revoke --id <credential-id>"
```

### `heroku pg:psql` is broken on this Windows install

It shells out to a bundled `psql` through a path containing a space and fails
before connecting:

```
'C:\Program' is not recognized as an internal or external command
```

Nothing to do with credentials or the database. Go around it with the project's
own interpreter, which needs no `psql` at all:

```powershell
$env:DATABASE_URL = (heroku config:get DATABASE_URL --app <app>)
.\.venv\Scripts\python.exe -c "import os, psycopg; u = os.environ['DATABASE_URL'].replace('postgres://','postgresql://',1); u += ('&' if '?' in u else '?') + 'sslmode=require'; c = psycopg.connect(u); print(c.execute('select version_num from vault.vault_alembic_version').fetchone())"
Remove-Item Env:DATABASE_URL
```

This is also the *better* check for "what schema is live", because it resolves
the URL exactly as the scripts do — see the next entry.

### `alembic current` needs `-c alembic-vault.ini`, or it answers the wrong question

There are two lineages. Without the flag you get the leaderboard one, which
tops out at `0004_auth_identities` — a plausible-looking answer to a question
you did not ask:

```bash
heroku run "python -m alembic -c alembic-vault.ini current" --app <app>
```

A vault revision reads `00NN_<name>`, currently `0015_note_compile_declined`.
If you see `0004_auth_identities`, you checked the leaderboard lineage.

### Which database a script is about to touch

Every script that writes, deletes, or exports now prints its target before
acting:

```
database   : ec2-…-1.compute.amazonaws.com:5432/d7abc…
```

Read that line. `VaultSettings` resolves `VAULT_DATABASE_URL` first and falls
back to `DATABASE_URL`, `load_environment` fills either from `.env` when the
process has none, and `$env:` variables die with the terminal — so "which
database am I talking to" has three possible answers and only this line reports
which one won.

A `--dry-run` or a missing `--apply` on `import_vault_wiki` **contacts no
database at all**; it returns before an engine is built. A passing dry run
therefore says nothing whatsoever about the target.

### PowerShell has no inline environment prefix

`DATABASE_URL=... python x.py` is a bash-ism. In PowerShell, set the variables as their own
statements first, use the venv interpreter explicitly, and clear them afterwards so a later
command in the same session does not quietly address production:

```powershell
$env:DATABASE_URL = (heroku config:get DATABASE_URL --app <app>)
$env:VAULT_ENABLED = 'true'; $env:PYTHONPATH = '.'
.\.venv\Scripts\python.exe <script>.py --apply
Remove-Item Env:DATABASE_URL, Env:VAULT_ENABLED, Env:PYTHONPATH
```

Reading the URL from `heroku config:get` beats pasting it: one fewer copy of a live
credential, and it survives rotation.

## Later move to a separate database

Provisioning or attaching another database is deliberately outside Phase 1.
When approved later:

1. Set `VAULT_DATABASE_URL` to the second database credential.
2. Set `VAULT_DATABASE_CONNECTION_LIMIT` to that plan's actual limit.
3. Recalculate the leaderboard and vault budgets independently.
4. Run the vault Alembic lineage against the new URL.
5. Move data through the reviewed schema-qualified export/restore procedure.

No route, repository, table definition, or transaction may assume that the
vault and leaderboard share a database.

## Extraction note

The `pgvector` Python package is required only by `app/vault/`. When the vault
runtime moves into the private knowledge-platform package, move that dependency
with it and remove it from HSS's manifest. Existing leaderboard code must not
import it.

The embedding adapter adds no package: it uses `httpx`, which HSS already
depends on for Steam ticket validation, so `httpx` stays in both manifests.

The `mcp` package is required only by `app/vault/mcp.py` and leaves with the
package, along with its transitive `mcp-types`, `opentelemetry-api`,
`truststore`, and `httpx2`/`httpcore2` — the last being a *second* HTTP stack
that installs alongside `httpx` rather than replacing it. See ADR 0021 and the
extraction manifest.

`VAULT_MCP_ALLOWED_HOSTS` is optional and unset by default. Naming hosts in it
(comma-separated) turns on the transport's DNS-rebinding protection, restricted
to those values. It is off by default because the SDK validates `Host` against
`127.0.0.1`, which would reject every request to a public deployment with 421.

## Changing embedding model or dimensions

Embeddings live in `vault.vault_document_embeddings`, keyed by
`(document_id, profile_id)` — see vault ADR 0003. That changes what a re-embed
costs, and the two cases are no longer the same operation.

### Same dimensions, different provider or model

**No migration.** The new profile is new rows. Both profiles coexist, so the old
one stays queryable for rollback and the two can be compared directly.

1. Select and evaluate the replacement profile. Record provider, exact
   model/revision, query/document modes, dimensions, and normalization
   behavior. Re-run retrieval fixtures: vectors from different profiles are not
   comparable even when their dimensions match.
2. Run a resumable operator backfill outside the release phase. Embed documents
   in bounded batches under the new `profile_id`, checkpointing progress without
   logging note bodies, credentials, or vectors. `upsert` is keyed on the
   primary key, so the job is safe to re-run.
3. Add a partial HNSW index for the new profile
   (`WHERE profile_id = '<literal>'`) before serving reads from it. The base
   index is unpartitioned, so with two populated profiles a profile filter is a
   post-filter and can cost recall.
4. Verify every searchable active document has a row under the new profile.
   Check counts, stable document IDs, exact-token and paraphrase fixtures,
   top-10 overlap, latency, and embedding failure metrics.
5. Switch query embedding and retrieval to the new profile in a separately
   reviewed release. Keep lexical retrieval active and retain the old profile's
   rows for rollback.
6. After the rollback window, delete the old profile's rows and drop its partial
   index. This is a `DELETE` plus a `DROP INDEX`, not a table rewrite.

### Different dimensions

`VAULT_EMBEDDING_DIMENSIONS` remains a checked deployment contract, not a
setting that resizes a pgvector column. At startup it must match the dimension
compiled into the current Core metadata and created by Alembic; changing it
alone intentionally fails fast.

`vault_document_embeddings.embedding` is `vector(1536)`, and HNSW requires a
fixed dimension, so a dimension change still requires reviewed DDL — the join
table removes the migration for model swaps, not for dimension swaps.

**The DDL shape for this is an open decision** and should be settled when a
dimension change is actually proposed rather than guessed at now. The plausible
options are a second vector column on the same table, or a parallel table per
dimension; which is better depends on whether the two dimensions need to be
served concurrently. Whichever is chosen: keep the historical migration literal
unchanged, do not alter a populated column in place, and never call an embedding
provider from Alembic.

For a small local or disposable database, dropping the rows and re-embedding is
acceptable. Production must use the additive path so a model change never
requires an irreversible release migration.

## Embedding provider

The provider evaluation is complete and the decision is recorded in vault ADR
0005. The first profile is **`openai/text-embedding-3-small:1536`**, reached
through the REST endpoint over `httpx` rather than the vendor SDK, so no new
package was added.

```
VAULT_EMBEDDING_PROVIDER=openai
VAULT_EMBEDDING_MODEL=text-embedding-3-small
VAULT_EMBEDDING_PROFILE_ID=openai/text-embedding-3-small:1536
VAULT_EMBEDDING_DIMENSIONS=1536
VAULT_EMBEDDING_TIMEOUT_SECONDS=5
# VAULT_EMBEDDING_BASE_URL=   # unset: each adapter supplies its own default
# VAULT_EMBEDDING_API_KEY=    # secret; Heroku config only, never a tracked file
```

### `VAULT_EMBEDDING_TIMEOUT_SECONDS` is per attempt, not per request

**This is the easiest value here to get wrong, and it was wrong in this file,
in `.env.example`, and in the deployed configuration until 2026-08-14.** The
embedding call retries, so the number an operator sets is not the number that
bounds the request:

```text
worst case = 3 attempts x <timeout> + 2 x 4s of capped backoff
```

| Setting | Worst case | Outcome |
| --- | --- | --- |
| **5 (default)** | **23s** | Fits, with 7s left for the rest of the request |
| 7.3 | 29.9s | The largest value that fits |
| 10 | 38s | Router 503s at 30s; the work is discarded |

**Startup now refuses anything whose budget exceeds 30s**, and the error prints
the arithmetic, the maximum that fits, and what to do instead. Before this guard
existed the only check was a unit test against the *default* constant, which
kept passing while the deployed value was 10 — a test cannot see what a
deployment configures.

Do not raise this to give slow calls more room. 5s is roughly **four times** the
measured single-query p99 of 1.194s, and because the timeout is per attempt a
slow call is *retried* rather than lost. The failure this budget exists to
survive is a transient 429 or 502, not slowness.

A batch backfill is the one legitimate reason to want longer, and it must not
change this variable: it has no caller waiting, so it passes `timeout_seconds`
to the provider directly. Only the environment is constrained.

Changing it means changing it in every place it is set — `.env`, `.env.example`,
the `heroku config:set` block above, and this table — because nothing reconciles
them.

`VAULT_EMBEDDING_PROFILE_ID` defaults to `{provider}/{model}:{dimensions}` and
is validated at startup against the same pattern as the
`vault_document_embeddings_profile_id_format` check constraint, so a typo fails
the boot rather than the first insert.

**`VAULT_EMBEDDING_API_KEY` is optional by design.** Without it the vault runs
lexical-only: startup logs a warning, `profile_id` is reported as null, and
every response carries `vector_status: "not_configured"`. CI and local
development run this way. Setting `VAULT_EMBEDDING_PROVIDER` to a name with no
adapter is a different case and fails loudly.

A configured provider that then fails is a third case and is reported as
`vector_status: "failed"`, with an ERROR logged carrying the exception type and
the profile — never the query text or the exception message, both of which can
quote note content. **Treat `"failed"` as an alert condition:** results are
silently narrower than they should be, and nothing else will tell you.

Layering choice is separate from configuration: `settings.py` parses these
variables and deliberately does **not** know which adapters exist, which is what
lets the Alembic environment import it without pulling in an HTTP client. The
registry in `embedding_runtime.py` is the only module that maps a provider name
to a concrete adapter.

Changing provider or model requires controlled re-embedding; it must never be
treated as a credentials-only configuration change. The procedure is above under
"Changing embedding model or dimensions".

## Credentials and scopes

Every surface is gated on operator-issued agent credentials, sent as
`Authorization: Bearer hssv1_<credential-id>_<secret>`:

```bash
python -m scripts.issue_vault_credential issue --name claude-code --scopes vault:read
python -m scripts.issue_vault_credential list
python -m scripts.issue_vault_credential revoke --id <credential-id>
```

Only the SHA-256 of each secret is stored, so the token is printed once and a
lost one is revoked and reissued rather than recovered. Issuing against
production means setting `DATABASE_URL` explicitly for the command — writing a
credential into the wrong database is silent.

### One scope per verb

| Scope | Grants |
| --- | --- |
| `vault:read` | Search, and fetch by id |
| `vault:write` | Contribute a new note — **and nothing else** |
| `vault:propose` | Submit an immutable, revision-bound full replacement or bounded body diff for review; does not edit the note |
| `vault:update` | Replace an existing note's content |
| `vault:delete` | Retire a note, **destroying it** (vault ADR 0019) |
| `vault:review` | List, read, and decide near-duplicate cases and amendment proposals. It applies accepted amendments and is **the only scope that serves `flagged` content**, so grant it narrowly |
| `vault:compile` | Plan, write, and settle wiki compilation runs; operator-granted only |
| `vault:export` | Recognised for the future export surface; currently granted by no route |

`vault:write` is contribute *only*. It gated all three write routes until vault
ADR 0020.

`vault:propose` and `vault:review` are deliberately separate capability profiles. An ordinary
agent may author inert proposals but cannot apply them. A reviewer should hold exactly
`vault:read vault:review`: it can inspect and decide stored proposals but cannot compose a new
change through `vault:propose`, directly replace through `vault:update`, or retire through
`vault:delete`. Keep the per-call scope checks and the MCP tool-list filtering; neither replaces
the other.

**A credential issued before 2026-08-15 holds `vault:write` alone**, so its
replace and retire calls now return `403`. Migration `0007_write_scope_split`
changes the schema and grants nothing, deliberately: a migration reruns, and one
that re-applies privilege would silently restore permissions on every rebuild,
rollback, or staging refresh. Widening an existing credential is a manual,
per-credential decision:

```sql
UPDATE vault.vault_agent_credentials
SET scopes = (
    SELECT array_agg(scope ORDER BY scope)
    FROM (SELECT unnest(scopes || ARRAY['vault:update']::text[]) AS scope) w
)
WHERE id = '<credential-id>';
```

Reissuing with exactly the scopes that client needs is better for anything
long-lived, and `issue_vault_credential.py` has always supported it.

The 2026-08-16 pre-merge production check found no credential table and thus no
production credentials to migrate. That is evidence for the current dark
deployment, not a permanent exemption: repeat the inventory immediately before
enablement, after migrations and before routes receive traffic.

**Grant `vault:delete` deliberately.** It is the only irreversible verb: ADR 0019
retirement leaves no archived row and nothing a caller can still resolve. An
importer-shaped client wants `vault:read vault:write vault:update` and not the
fourth:

```bash
python -m scripts.issue_vault_credential issue --name importer `
  --scopes vault:read vault:write vault:update
```

Credentials do not expire unless `--days` is given, which is deliberate for the
machine clients this serves — an expiry that lapses unnoticed is an outage, and
revocation is immediate and needs no cache to expire. Use `--days` for anything
handed to a third party or issued for one task.

### Browser and CORS contract

The current vault write clients are operator-issued agents and projectors, not
browser applications. They call the HTTP API as machine clients, so browser CORS
preflight intentionally does not advertise `PUT` or `DELETE`. HighScoreServer's
global CORS policy remains scoped to its public leaderboard/auth browser surfaces
(`GET`, `POST`, and `OPTIONS`). This is not an authorization boundary — every
vault route still requires an agent credential — but it avoids promising a
browser integration that does not exist. If a browser vault client is added,
widen CORS deliberately and add preflight coverage in the same change.

The vault cannot reuse HighScoreServer's authentication — importing it would
breach the isolation rule that keeps extraction a directory move — and the
integration spec is explicit that player JWTs and the leaderboard `API_KEY` are
not vault credentials. A request with no credential, an unknown credential, or
a revoked or expired one is `401`; one that authenticates but lacks the scope
the route requires is `403`. Neither response says which check failed. See vault
ADR 0015 and 0020.

Routes are registered only when `VAULT_ENABLED` is true, so a disabled vault
publishes no endpoints and no OpenAPI schema. They are mounted under
`/api/v1/vault`, ahead of the SPA catch-all and the static-file mount.

Rate limiting is **two layers**, both in `app/vault/rate_limit.py`.

The **quota** is enforced per authenticated principal by a vault-local token
bucket. Exceeding one returns `429` with `Retry-After` in whole seconds.

| Operation | Sustained | Burst |
| --- | --- | --- |
| `search` | 30/min | 10 |
| `get_note` | 120/min | 30 |
| `contribute` | 30/min | 20 |
| `amendment_propose` | 30/min | 20 |
| `amendment_list` | 60/min | 20 |
| `amendment_read` | 60/min | 20 |
| `amendment_decide` | 10/min | 5 |
| `update` | 30/min | 20 |
| `retire` | 10/min | 5 |
| `review_list` | 60/min | 20 |
| `review_read` | 60/min | 20 |
| `review_decide` | 10/min | 5 |
| `compile_plan` | 6/min | 3 |
| `compile_write` | 30/min | 20 |
| `compile_settle` | 10/min | 5 |
| `snapshot` | 2/hour | 1 |

`retire`, `review_decide`, and `amendment_decide` have tight decision buckets because they
destroy, publish, or replace corpus content. `compile_plan` is tighter still in sustained rate
because every abandoned plan leaves a running workflow row; page writes retain batch headroom.

The **pre-auth guard** is IP-keyed and charged *before* the credential is looked
up, because verifying a credential is itself a database round trip and the quota
cannot cover the cost of the check that produces its own key. It is a slowapi
`Limiter` owned by the vault — a third-party import, not a host import, so the
isolation rule is intact and this instance is independent of HSS's. Defaults to
`600/minute`, deliberately loose: it is a floor that stops anonymous hammering,
not a quota, and one egress address may legitimately carry several credentials.

The deployment topology is direct client → Heroku router → dyno. Heroku appends
the address it observes to the **right** of any existing `X-Forwarded-For` list,
so the guard keys on the rightmost value and ignores caller-controlled prefixes.
Heroku also warns that forwarded headers as a whole are unsuitable authorization
inputs; this address is only an abuse-control bucket. If a CDN or other proxy is
placed in front of Heroku, the rightmost value identifies that proxy and this
assumption must be revisited. See Heroku's
[HTTP routing documentation](https://devcenter.heroku.com/articles/http-routing#heroku-headers).

| Variable | Default | Purpose |
| --- | --- | --- |
| `VAULT_PREAUTH_RATE_LIMIT` | `600/minute` | Per-IP ceiling before authentication |
| `VAULT_RATE_LIMIT_STORAGE_URI` | `memory://` | Set to `REDIS_URL` to share across workers and dynos; falls back to in-memory if Redis is unreachable |

The guard is attached as a **router-level dependency**, not a route decorator.
FastAPI solves dependencies before calling the endpoint and authentication is a
dependency, so a decorator would charge after the round trip it exists to
prevent. Do not "simplify" it into a decorator.

`search`, `get_note` and `snapshot` match the integration spec. **`contribute`
deliberately does not.** The spec's 10/min burst 3 assumes contributions trickle
in; they arrive in batches instead — a librarian session settling nine notes, an
importer replaying a corpus of fifty — so burst 3 throttled every real run
without touching the abuse case, which is sustained rate. `update` takes the
same shape in its own bucket, so a corpus-wide backfill cannot starve new
contributions. The reasoning is on `LIMITS` in `rate_limit.py`.

Raising the burst does **not** make concurrent writes fast. The governed write
path holds a corpus-wide advisory lock, so simultaneous writes serialize on it
rather than on the limiter. `VAULT_DB_POOL_SIZE` was a second serializer until it
moved to 2; at 1 a concurrent write failed on the pool timeout instead of
queueing. The burst makes *sequential* batches fast, which is what the only
client actually does.

**The quota's buckets are per process.** Each Gunicorn worker holds its own, so
the effective ceiling is the stated limit times the worker count — two,
currently. That is a known factor on a single host. Across hosts it stops being a
limit at all, which is the point at which a shared backend becomes necessary
rather than tidier. The pre-auth guard can already take one via
`VAULT_RATE_LIMIT_STORAGE_URI`; the quota cannot, and would need the same
treatment.

## Granting an agent access, end to end

Everything from "someone wants to use the vault" to "their agent is contributing
notes". Do it once per person or per machine.

### 1. Decide the scopes

Scopes are verbs, one per route (vault ADR 0020), and they are what shapes the
MCP tool surface a caller can see (ADR 0021). Grant the narrowest set that does
the job:

| Who | Scopes | Why |
| --- | ------ | --- |
| An ordinary agent or person contributing notes | `vault:read vault:write vault:propose` | Search, fetch, contribute, and suggest consolidation without edit authority |
| A read-only consumer | `vault:read` | Retrieval with no way to write |
| A corpus import or backfill | `vault:read vault:write vault:update` | Replacement is a separate verb |
| A reviewer adjudicating flagged notes or amendments | `vault:read vault:review` | Reads untrusted proposal previews, applies accepted amendments, and **serves `flagged` content**; keep it separate from ordinary agent credentials |
| Nobody, by default | `vault:delete` | Retirement destroys a note (ADR 0019). Grant per incident, revoke after |

Withholding a scope is a **prompt-injection boundary, not tidiness**. The MCP
tool list is filtered by the caller's scopes, so a credential without
`vault:delete` has no `vault_retire_note` on its surface at all — there is no
tool for injected text in a note to name. Do not fold scopes together because
issuing two looks like ceremony; that is exactly how `vault:write` came to mean
"may destroy any note" before ADR 0020 split it.

### 2. Mint the credential

```bash
python -m scripts.issue_vault_credential issue --name alice-laptop --scopes vault:read vault:write vault:propose
```

Against production, set `DATABASE_URL` explicitly for the command — issuing into
the wrong database is silent. On a dyno it is already set:

```bash
heroku run --app <app> "python -m scripts.issue_vault_credential issue --name alice-laptop --scopes vault:read vault:write vault:propose"
```

Quote the whole remote command. `heroku run` parses the line first and claims
`-m` and `--scopes` for itself otherwise, reporting `Nonexistent flags` about
flags that are perfectly valid for your script.

**Name it per person or per machine, never per team.** `contributed_by` is
derived from the principal and never from the request body, so the name becomes
the note's `ContributedBy` — one credential per person gives per-person
provenance in the corpus and per-person revocation, both for free. A shared
credential gives up both and cannot be taken back from one user.

`--days N` sets an expiry. Omit it for none.

**The token prints once and is not recoverable** — only `sha256(secret)` is
stored. Two consequences:

- Have the *person* run this in their own shell. An agent that runs it has read
  the secret into its transcript, and from there into summaries and logs. This
  has already happened twice to live production credentials.
- If it leaks, revoke and reissue. Do not reason about how exposed it is.

### 3. Register the MCP server (preferred)

**One line. No continuation character.**

```
claude mcp add --transport http --scope user vault https://example.herokuapp.com/api/v1/vault/mcp/ --header "Authorization: Bearer hssv1_<credential-id>_<secret>"
```

A trailing `\` is a bash-ism PowerShell does not honour. The command truncates
there, the server registers *without* the `--header`, and the only symptom is a
later `✘ Failed to connect` that names nothing useful. That has already happened
once, from this very runbook — which at the time also carried a section warning
that PowerShell needs different syntax.

Substitute the real host. A literal `<host>` registers as a literal `<host>`,
which is the second half of the same incident.

`--scope user` makes the server available in every project. The default is
`local`, meaning the current project only — rarely what you want for a vault.

**Note the trailing slash** on the URL. The bare form 307-redirects to it, so
both work and the slash saves a hop.

This only stores a URL and a header — nothing is launched. The vault MCP server
is an ASGI app mounted into the host application, so it is already running
wherever the host runs; there is no process to start and no Procfile entry.

The credential lives in the client's configuration and the transport attaches
it, so **the agent never handles the token**. That is the reason to prefer this
over REST: not convenience, but that there is no secret in the session to leak.

### 4. Or configure REST (fallback)

For agents without MCP — Codex, CI, one-off scripts. The token goes in an
environment variable the process inherits:

```bash
export VAULT_API_TOKEN='hssv1_<credential-id>_<secret>'
```

Never on a command line: argv is readable by every process on the host. Never in
a file the agent writes, and never printed.

Endpoints are `GET /api/v1/vault/search`, `GET /api/v1/vault/notes/{id}`,
`POST /api/v1/vault/contributions`, `PUT /api/v1/vault/notes/{id}`, and
`DELETE /api/v1/vault/notes/{id}`, plus the amendment workflow below. All take
`Authorization: Bearer <token>`.

| Method and path | Scope | Purpose |
| --- | --- | --- |
| `POST /api/v1/vault/amendment-proposals` | `vault:propose` | Store an inert, revision-bound proposal |
| `GET /api/v1/vault/amendment-proposals` | `vault:review` | List pending proposals without their change bodies |
| `GET /api/v1/vault/amendment-proposals/{proposal_id}` | `vault:review` | Read the stored change, current target, and materialized preview |
| `POST /api/v1/vault/amendment-proposals/{proposal_id}/decision` | `vault:review` | Accept or reject the exact stored change |

An amendment request carries a discriminated `change`. Use
`{"kind":"body_diff","body_diff":"..."}` for a compact unified diff against the body.
Hunks may add, edit, or remove lines but must anchor to exact existing text. The service refuses
patches over 50,000 characters, 20 hunks, 200 changed lines, or the per-note 25%/20-line budget;
use `{"kind":"replacement","replacement":{...all content fields...}}` for metadata or
larger changes. Both forms require the note's current `content_revision` as `base_revision`; a
mismatch returns 409 at proposal time and settles stale at review time.

`replacement` uses the same complete caller-controlled content shape as `PUT /notes/{id}`;
omitted optional fields are cleared rather than inherited:

```json
{
  "kind": "replacement",
  "replacement": {
    "title": "Complete title",
    "body": "Complete body",
    "summary": null,
    "tags": [],
    "aliases": [],
    "facets": {},
    "related_ids": [],
    "source_ids": [],
    "source_url": null
  }
}
```

Reading a pending proposal returns `preview`: the complete resulting body, canonical unified
diff, and an explicit list of removed lines with their original line numbers. The preview is
null when the target is missing or no longer at the base revision. Accepting a proposal whose
preview reports removals requires `"acknowledge_removals": true`; the settled record preserves
that acknowledgement in `proposal.removals_acknowledged`.

The complete REST flow, with secrets omitted, is:

```http
POST /api/v1/vault/amendment-proposals
Content-Type: application/json

{
  "target_note_id": "note-id",
  "base_revision": 3,
  "change": {
    "kind": "body_diff",
    "body_diff": "@@ -4,2 +4,2 @@\n-old guidance\n+corrected guidance\n context"
  },
  "rationale": "The old command is no longer valid."
}
```

Submission returns a pending proposal id. The reviewer lists the queue, then reads only the
selected proposal:

```http
GET /api/v1/vault/amendment-proposals/{proposal_id}
```

Its `preview` contains `resulting_body`, `unified_diff`, `added_line_count`, `removed_lines`
with original line numbers, `removed_line_count`, `hunk_count`, and
`requires_removal_acknowledgement`. `preview` is null if the target is missing or no longer at
`base_revision`; the service never previews a silent rebase.

After reviewing the complete result and removal summary:

```http
POST /api/v1/vault/amendment-proposals/{proposal_id}/decision
Content-Type: application/json

{
  "decision": "accepted",
  "decision_note": "Verified against the current tool behavior.",
  "acknowledge_removals": true
}
```

Omit `acknowledge_removals` when rejecting or when the preview reports no removals. Attempting
to accept a removal without it returns 409 and leaves the proposal pending. If the target
changes after preview, acceptance returns a settled `stale` outcome and writes no corpus
content. A successful acceptance reports the new `content_revision` and persists
`removals_acknowledged`; rejection and staleness do not require an embedding provider.

### 5. Verify

```bash
curl -sS -H "Authorization: Bearer $VAULT_API_TOKEN" "https://<host>/api/v1/vault/search?q=idempotency&limit=3"
```

On PowerShell that is `curl.exe` — the bare `curl` is an alias for
`Invoke-WebRequest`, which takes different arguments and throws on a non-2xx
rather than printing the body. Use `$env:VAULT_API_TOKEN` for the variable.

A working credential returns results and a `vector_status`.

For MCP, check the registration itself first:

```
claude mcp list
```

`✔ Connected` means the URL and header both landed. `✘ Failed to connect` most
often means the header did not — see the truncation trap above.

There is **no edit subcommand**. Changing a registration is remove-then-add:

```
claude mcp remove vault
```

Then check the tools: the ones that appear should match the scopes granted — a
`vault:read vault:write vault:propose` credential shows `vault_search`, `vault_get_note`,
`vault_contribute`, `vault_propose_note_amendment`, and `vault_propose_note_body_diff`, and no
privileged tools. The body-diff tool handles focused additions, edits, and removals; use the
full amendment tool for metadata or large changes. **A server added mid-session does not
appear in that session**; the tool set is fixed at startup, so restart the agent
before concluding the registration failed.

### 6. Rotate, revoke, and change scopes

```bash
python -m scripts.issue_vault_credential list
python -m scripts.issue_vault_credential revoke --id <credential-id>
```

`list` shows each credential's principal, scopes, creation, expiry, revocation,
and `last_used_at` — which means "last used", not "last attempted", because it
is written only on success. A credential that has never been used shows `never`,
which is how a registration that silently failed becomes visible.

Rotation is revoke-then-issue; there is no re-key, because the secret was never
stored.

#### Changing static credentials without rotating

```bash
python -m scripts.issue_vault_credential grant --id <credential-id> --scopes vault:update
python -m scripts.issue_vault_credential revoke-scope --id <credential-id> --scopes vault:update
```

These commands change what a static credential may do and leave its secret alone, so
nothing has to be redistributed — that is the difference from revoke-then-issue,
and the reason to reach for these instead.

Both print the scope set before and after. Both are additive/subtractive rather
than a replacement: `grant` never removes a scope the operator did not name, and
naming a scope already held (or already absent) reports `No change` and writes
nothing. They deliberately refuse OAuth-minted credential ids: changing one
rotating row would disappear at refresh while looking permanent to the operator.

#### Persistently entitling one OAuth authorization

OAuth caps what a client may request at `vault:read`, `vault:write`, and
`vault:propose`. Above-baseline authority is granted by an operator to one
refresh family, never requested by the client:

```bash
python -m scripts.issue_vault_credential grant-oauth --id <credential-id> --scopes vault:update
python -m scripts.issue_vault_credential revoke-oauth-scope --id <credential-id> --scopes vault:update
```

The id may name the current credential or an older rotated credential in the
same family. It is a lookup handle, not the persistence target. The command
prints the client id, grant family, and entitlement set before and after;
updates the current access credential and refresh token immediately; and
records an operator audit event. Every later refresh recomputes the credential
from the consented baseline plus the current entitlements.

This is intentionally narrower than granting a client registration. A new
browser authorization creates a new family and inherits no privileged scopes,
even when it uses the same registration. Revocation of an entitlement narrows
the live token and future rotations but leaves the OAuth session active.

`vault:review` has an additional separation-of-duties guard: it can be granted
only to a separately authorized family holding `vault:read` alone. The final
set is exactly `vault:read vault:review`. Do not widen an ordinary
read/write/propose agent into a reviewer. Likewise, prefer distinct families
for importer (`read`, `write`, `update`), compiler (`read`, `compile`), and any
future exporter (`read`, `export`) rather than accumulating roles on one agent.

Baseline scopes are not legal entitlements, and OAuth cannot request
`vault:update`, `vault:delete`, `vault:review`, `vault:compile`, or
`vault:export`. Before these commands existed, widening meant a hand-written
database update; that is no longer a supported production procedure.

Three refusals worth knowing about:

- **A revoked static credential is refused, not widened.** Scopes on a revoked row grant
  nothing, and an operator reaching for `grant` there is plausibly hoping it will
  un-revoke the credential. It will not, so the command says so instead of
  succeeding silently. Issue a new credential.
- **An expired static credential is refused** for the same reason. OAuth
  entitlement commands instead require a live refresh token in the family; a
  dead family must reauthorize and receive a new, deliberately granted role.
- **An unknown scope name is refused before any write**, with the list of real
  ones. The database CHECK would catch it too, but as an integrity error naming a
  constraint.

Removing every scope is allowed and is **not** the same as revoking: the
credential still authenticates, and each route then refuses it with `403` rather
than `401`. The command says so when it happens, because an operator who meant to
revoke needs to know they have not.

### Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `401` | Bad, expired, or revoked credential. The response deliberately does not say which |
| `403` | Valid credential, missing scope. Check the grant against the table above |
| MCP tools missing entirely | Server not registered for this client, or the header is wrong. The mount carries its own auth — it inherits none of the host router's guards |
| Some MCP tools missing | Working as designed: the tool list is filtered by scope |
| `421` on every MCP request | `VAULT_MCP_ALLOWED_HOSTS` is set and does not name this host. Unset it, or add the host. It is off by default because the SDK's default validates `Host` against `127.0.0.1` and would reject every request to a public deployment |
| `429` | Quota, per principal per operation. Buckets are per process, so the real ceiling is the limit times the worker count |
| `503` on a write | No embedding provider, so the dedup gate cannot run. The write path refuses rather than inserting un-deduplicated content |
| `409` on a contribution | An idempotency key was reused for different content. Change one or the other; do not loop |
| A client reports the server does not support OAuth | Discovery is not answering on the URL it was configured with. Check both well-known forms — RFC 9728 derives the metadata path from the resource path, so `…/mcp` and `…/mcp/` are two different documents — and check the logs for the `VAULT_PUBLIC_URL` drift warning |
| `column … does not exist` from a script | The target database is behind the code. Confirm the vault lineage with `-c alembic-vault.ini`, and read the `database :` line the script prints — a stale `$env:DATABASE_URL`, or none at all, silently falls back to `.env` |

A `flagged` result is **not** an error. It is a settled `200` outcome meaning the
note was written for review, and retrying it creates a second note that flags
against the first.

## Saturation

An exhausted vault pool — every connection checked out, `pool_timeout` elapsed —
raises `sqlalchemy.exc.TimeoutError`, which the application maps to **`503` with
`Retry-After`**, not `500`. Saturation is a load condition, not a defect: a `500`
would tell the caller not to retry something purely transient, and an error
tracker to report a bug where the truth is that the vault is busy. A rise in
these is a signal to raise `VAULT_DB_POOL_SIZE`, which means revisiting the
budget above.

### Observing the pool

`VaultPoolObserver` counts checkouts per worker and the application logs it, so
the enablement review's "repeat the connection review under vault traffic" is a
log search rather than a load-testing exercise:

```bash
heroku logs --app high-score-server --tail | grep "Vault pool"
```

```text
Vault pool interval: peak 2/2 concurrent, 0 failures
Vault pool final: peak 2/2 concurrent, 0 failures
```

Two fields carry the answer. **`peak` is a running maximum of simultaneous
checkouts**, not a sample — it is recorded at the moment it happens, because a
peak cannot be recovered afterwards by polling a gauge. **`failures` counts
checkouts that waited out `pool_timeout` and were refused**, which is the path
that becomes a `503`; a non-zero value raises the line to `WARNING`, so
`grep -i "vault pool.*[1-9] failures"` finds every occurrence without reading
numbers. Peak at capacity with zero failures means the pool was fully used and
still sufficient; any failures mean it was not.

`VAULT_POOL_LOG_INTERVAL_SECONDS` sets the cadence, default 300. Because the
maxima are cumulative, the interval decides how much trend you see rather than
whether the peak is captured — a peak at any point appears in every later line.
Set `0` to log only at shutdown.

**Two limitations, both structural.** The counters are **per worker**: each
process sees only its own checkouts, which matches how the budget is expressed
(pool size × workers) but means the database-wide total — including the release
dyno and any operator session — is invisible here. Use `pg_stat_activity` for
that. And the closing line depends on a **graceful** shutdown; Heroku's `SIGTERM`
produces one, but a dyno killed after the grace period expires loses it, which
is the reason to keep the interval lines rather than relying on shutdown alone.

```bash
heroku pg:psql --app high-score-server -c "SELECT count(*) AS total, count(*) FILTER (WHERE state='active') AS active FROM pg_stat_activity WHERE datname = current_database();"
```
