"""Stable contracts shared across the vault package.

Mostly persistence contracts. Anything here must be importable by the Alembic
environment, so this module never imports a transport client.
"""

import os
import re


# Changing this value requires a new Alembic column/index migration and a
# controlled re-embedding job. Historical migrations intentionally keep their
# literal dimension so changing application code cannot rewrite old DDL.
EMBEDDING_DIMENSIONS = 1536

DEFAULT_TEXT_SEARCH_CONFIG = "english"

# Facet name and value length ceilings. Persistence contracts: the name limit is
# restated by the `vault_documents_facets_shape` CHECK in migration
# 0005_document_facets, so the two must agree. Values are not length-checked in
# the database -- a JSONB object is bounded overall and a per-element CHECK
# would scan every value on every write for a limit application code already
# enforces. See ADR 0017.
MAX_FACET_NAME_LENGTH = 64
MAX_FACET_VALUE_LENGTH = 128

# The default per-request embedding timeout, in seconds.
#
# Not a persistence contract like the two above, but it lives here for the same
# reason: three places have to agree on it — the environment default in
# settings, the adapter's parameter default, and the test that proves the retry
# budget fits inside Heroku's 30s router timeout. This module is the only one
# all three can import without pulling a transport client into the Alembic
# environment.
#
# Changing this changes the worst case that test models. See "Deferred
# decisions" item 3 in docs/vault-architecture.md before touching it.
#
# The test only models the *default*, which is not what a deployment runs under
# -- 10 was configured everywhere for months while it passed. The configured
# value is bounded separately, in EmbeddingSettings.from_environment.
#
# 5.0 since 2026-08-12, measured rather than reasoned: single-query latency
# against the real API is p50 0.163s, p99 1.194s, and a 128-document batch takes
# 0.728s. A 5s ceiling is roughly four times the observed p99, so it converts
# essentially no slow-but-successful call into a failure, and it buys room for
# three attempts inside the router budget. The previous 10.0 was chosen before
# any measurement existed.
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 5.0

# The retry shape, and the ceiling it has to fit inside.
#
# These live here rather than in the adapter that uses them because the *budget*
# is a property of the deployment, not of a vendor: settings has to validate the
# configured timeout against it, and settings deliberately imports no transport
# module (see EmbeddingSettings). The adapter reads them back out.
MAX_EMBEDDING_ATTEMPTS = 3
# Caps any Retry-After the provider sends. Load-bearing for the budget below:
# two waits at this cap are 8 of the 23 seconds.
MAX_EMBEDDING_BACKOFF_SECONDS = 4.0
# Heroku's router gives up here. A response that arrives later is not a slow
# success, it is a request the caller never receives.
ROUTER_TIMEOUT_BUDGET_SECONDS = 30.0


def embedding_retry_budget_seconds(timeout_seconds: float) -> float:
    """Worst-case wall time for one embedding call including its retries.

    Every attempt may burn a full request timeout, and a wait separates each
    pair of them — so N attempts means N timeouts and N-1 waits.
    """

    return MAX_EMBEDDING_ATTEMPTS * timeout_seconds + (
        (MAX_EMBEDDING_ATTEMPTS - 1) * MAX_EMBEDDING_BACKOFF_SECONDS
    )


def max_embedding_timeout_seconds() -> float:
    """The largest per-attempt timeout whose full budget still fits the router."""

    return (
        ROUTER_TIMEOUT_BUDGET_SECONDS
        - (MAX_EMBEDDING_ATTEMPTS - 1) * MAX_EMBEDDING_BACKOFF_SECONDS
    ) / MAX_EMBEDDING_ATTEMPTS

# The configuration name is interpolated into DDL as a literal, so it is
# constrained to the shape of an unquoted PostgreSQL identifier before it can
# reach a SQL string. Catalog validation against pg_ts_config happens in the
# migration, which is the only place a connection is available.
_TEXT_SEARCH_CONFIG_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def resolve_text_search_config() -> str:
    """Return the validated text search configuration name from the environment.

    The value is baked into the ``search_vector`` generated column at migration
    time. Changing it afterwards requires a table rewrite and a GIN reindex, not
    a restart.
    """

    value = os.environ.get(
        "VAULT_TEXT_SEARCH_CONFIG",
        DEFAULT_TEXT_SEARCH_CONFIG,
    ).strip()
    if not _TEXT_SEARCH_CONFIG_PATTERN.fullmatch(value):
        raise RuntimeError(
            "VAULT_TEXT_SEARCH_CONFIG must match ^[a-z_][a-z0-9_]*$; "
            f"got {value!r}"
        )
    return value
