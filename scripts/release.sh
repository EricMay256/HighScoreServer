#!/usr/bin/env bash
# Heroku release phase.
#
# A non-zero exit aborts the release, which is the whole point: a failed
# migration must never reach a running dyno. `set -e` is what enforces that,
# and `pipefail` keeps a failure from being swallowed by a pipe.
set -euo pipefail

# Leaderboard lineage. Always runs; it owns the public schema the app cannot
# start without.
alembic upgrade head

# Vault lineage. Gated on VAULT_ENABLED rather than unconditional, and the
# reason is not tidiness:
#
#   0001_vault_foundation runs CREATE EXTENSION vector. If pgvector is not
#   available on the attached Postgres plan that statement fails, and an
#   unconditional release phase would then abort EVERY deploy -- including
#   deploys with nothing to do with the vault. That is a far worse failure
#   than the gap this closes.
#
# Gating costs nothing, because setting VAULT_ENABLED=true itself triggers a
# release. The cutover is therefore exactly when the vault schema is built,
# and a failure aborts that release rather than an unrelated one.
#
# Verify pgvector on the target plan before flipping the flag:
#   heroku pg:psql --app <app> -c \
#     "SELECT name, installed_version FROM pg_available_extensions WHERE name='vector';"
if [ "${VAULT_ENABLED:-false}" = "true" ]; then
    alembic -c alembic-vault.ini upgrade head
fi
