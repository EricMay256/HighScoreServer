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

# The governance note-document schema a row was created under, per `types.yml`
# and `vault_governance.validate`. This is the *document* schema -- the shape of
# a note's frontmatter -- and not a version of the database row or of the request
# contract. Those are versioned by Alembic and by REQUEST_DIGEST_VERSION
# respectively, and conflating the three is how `vault_documents.schema_version`
# came to read 1 on every row while the corpus it replicated said 2.
NOTE_SCHEMA_VERSION = 2
WIKI_SCHEMA_VERSION = 1

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


# ── The OAuth authorization server (ADR 0024) ──────────────────────────────

# What a self-registering client receives, and the most it may request. ADR
# 0024 makes this a *baseline*, not a ceiling: a client can never ask for more,
# so `vault:update`, `vault:delete` and `vault:review` are unreachable by
# request rather than by an operator declining on a consent screen -- but the
# credential OAuth mints is an ordinary row, and an operator may widen a
# specific one afterwards. Above-baseline scopes are granted deliberately,
# never requested.
#
# Restricting the web path to read and write is a security decision rather than
# a convenience one: ADR 0021's defence against injected instructions is that a
# destructive tool is absent from the surface untrusted note text can name, and
# a web-authorized client has no retire tool to be talked into using.
OAUTH_BASELINE_SCOPES: tuple[str, ...] = ("vault:read", "vault:write")

# How long an authorization may sit waiting for the operator to finish the login
# form. Generous for a person reading a consent screen and typing a password,
# short enough that an abandoned attempt is not a standing invitation. Not
# configurable: a deployment that widened it would be weakening a security
# boundary through an environment variable, which is the mistake
# PRINCIPAL_LIMITS is kept out of configuration to avoid.
PENDING_AUTHORIZATION_TTL_SECONDS = 300

# How long a minted authorization code survives before /token must redeem it.
# RFC 6749 recommends a maximum of ten minutes and "a maximum of 1 minute" for
# new work; the redemption is a machine-to-machine round trip that happens
# immediately, so 60s is generous rather than tight.
AUTHORIZATION_CODE_TTL_SECONDS = 60
