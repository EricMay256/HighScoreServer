"""Vault configuration and database connection-budget validation."""

import math
import os
import re
from dataclasses import dataclass

from sqlalchemy.engine import make_url

from .constants import (
    DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
    EMBEDDING_DIMENSIONS,
    MAX_EMBEDDING_ATTEMPTS,
    MAX_EMBEDDING_BACKOFF_SECONDS,
    ROUTER_TIMEOUT_BUDGET_SECONDS,
    embedding_retry_budget_seconds,
    max_embedding_timeout_seconds,
    resolve_text_search_config,
)


# Mirrors the vault_document_embeddings_profile_id_format check constraint.
# Validating here turns a typo into a startup error instead of an integrity
# error on the first write, long after the value was chosen.
_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{3,128}$")

# The provider name is a registry key and a component of the default
# profile_id, so it is constrained to the same alphabet the profile allows.
_PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Invalid boolean value: {value!r}")


def _positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise RuntimeError(f"{name} must be one or greater")
    return parsed


def _positive_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return parsed


def _validate_embedding_timeout(timeout_seconds: float) -> float:
    """Reject a per-attempt timeout whose retries outlast the router.

    The timeout is per *attempt*, so the number an operator sets is not the
    number that matters — the budget is that timeout times the attempt count,
    plus the waits between them. Setting 10 looks like a 10s ceiling and is
    really 38s, which is past the point where the caller has already been given
    a 503 by the router and the work is thrown away.

    Checked here rather than only in a test because a test can only assert the
    *default*, and the default is not what production runs under. Only the
    environment is constrained: an adapter constructed directly with a longer
    ``timeout_seconds`` is a backfill with no caller waiting, which is a
    legitimate case this must not block.
    """

    budget = embedding_retry_budget_seconds(timeout_seconds)
    if budget <= ROUTER_TIMEOUT_BUDGET_SECONDS:
        return timeout_seconds

    raise RuntimeError(
        f"VAULT_EMBEDDING_TIMEOUT_SECONDS={timeout_seconds:g} does not fit the "
        f"request budget. The timeout is per attempt, so the worst case is "
        f"{MAX_EMBEDDING_ATTEMPTS} x {timeout_seconds:g}s + "
        f"{MAX_EMBEDDING_ATTEMPTS - 1} x {MAX_EMBEDDING_BACKOFF_SECONDS:g}s of "
        f"capped backoff = {budget:g}s, and the router gives up at "
        f"{ROUTER_TIMEOUT_BUDGET_SECONDS:g}s. A response that late is not a slow "
        f"success, it is one the caller never receives. The largest value that "
        f"fits is {max_embedding_timeout_seconds():.1f}s; the measured default is "
        f"{DEFAULT_EMBEDDING_TIMEOUT_SECONDS:g}s, which is about four times the "
        f"observed single-query p99 of 1.194s. If this is a batch backfill, it "
        f"has no caller waiting: pass timeout_seconds to the provider directly "
        f"instead of raising this variable."
    )


def vault_enabled() -> bool:
    """Whether the vault runtime is switched on for this process.

    Separate from ``VaultSettings.from_environment`` so route registration can
    ask the question without requiring a database URL to be present.
    """

    return _parse_bool(os.environ.get("VAULT_ENABLED", "false"))


def operator_password_hash() -> str | None:
    """The bcrypt hash the login form verifies against, or None if unset.

    Configuration rather than a table, which is a deliberate fork. There is
    exactly one operator secret, it has no lifecycle a schema would model, and
    rotating it is ``heroku config:set`` -- which is also the revocation story.
    A row would additionally put a human password hash into a database whose
    backups circulate more widely than a config var does.

    None is a supported state, and it means the password identity method is not
    configured for this deployment. It must never be treated as "any password
    works": the caller refuses the login outright, in the same way
    ``vault_enabled`` defaulting to false serves no vault rather than an
    unguarded one. Returning None rather than raising keeps this checkable
    without making an unconfigured deployment fail at startup, since ADR 0024
    also builds a Google path that needs no password at all.

    Shape is not validated here. A malformed hash is caught by
    ``passwords.verify_password``, which logs the fault and reports a failed
    login -- one message for every failure, per ADR 0024. Validating at startup
    would be a second place to keep the bcrypt format definition.
    """

    value = (os.environ.get("VAULT_OPERATOR_PASSWORD_HASH") or "").strip()
    return value or None


def normalize_sqlalchemy_url(url: str) -> str:
    """Normalize a PostgreSQL URL for SQLAlchemy's psycopg dialect."""

    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _database_target(url: str) -> tuple[str | None, int | None, str | None]:
    parsed = make_url(normalize_sqlalchemy_url(url))
    return parsed.host, parsed.port, parsed.database


@dataclass(frozen=True, slots=True)
class ConnectionBudget:
    shared_database: bool
    hss_allocated: int
    vault_allocated: int
    combined_allocated: int | None
    hss_limit: int
    vault_limit: int
    hss_required_unallocated: int
    vault_required_unallocated: int


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """Parsed embedding configuration, with no knowledge of which adapters exist.

    Whether an adapter is registered for ``provider`` is settled in
    ``embedding_runtime``. Keeping that check out of here is what lets the
    Alembic environment import settings without pulling in a transport client.
    ``base_url`` is ``None`` when unset so each adapter supplies its own
    default rather than this module favouring one vendor's endpoint.
    """

    provider: str
    api_key: str | None
    base_url: str | None
    model: str
    profile_id: str
    embedding_dimensions: int
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "EmbeddingSettings":
        provider = os.environ.get("VAULT_EMBEDDING_PROVIDER", "openai").strip()
        if not _PROVIDER_PATTERN.fullmatch(provider):
            raise RuntimeError(
                "VAULT_EMBEDDING_PROVIDER must match "
                f"{_PROVIDER_PATTERN.pattern}; got {provider!r}"
            )

        embedding_dimensions = _positive_int(
            "VAULT_EMBEDDING_DIMENSIONS",
            os.environ.get(
                "VAULT_EMBEDDING_DIMENSIONS",
                str(EMBEDDING_DIMENSIONS),
            ),
        )
        if embedding_dimensions != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                "VAULT_EMBEDDING_DIMENSIONS does not match the migrated "
                f"vector({EMBEDDING_DIMENSIONS}) schema. Changing dimensions "
                "requires an Alembic migration and controlled re-embedding."
            )

        model = os.environ.get(
            "VAULT_EMBEDDING_MODEL",
            "text-embedding-3-small",
        ).strip()
        if not model:
            raise RuntimeError("VAULT_EMBEDDING_MODEL must not be empty")

        # Provider, model, and dimensionality named together: two vectors are
        # comparable only when their profiles match.
        profile_id = os.environ.get(
            "VAULT_EMBEDDING_PROFILE_ID",
            f"{provider}/{model}:{embedding_dimensions}",
        ).strip()
        if not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise RuntimeError(
                "VAULT_EMBEDDING_PROFILE_ID must match "
                f"{_PROFILE_ID_PATTERN.pattern}; got {profile_id!r}"
            )

        base_url = os.environ.get("VAULT_EMBEDDING_BASE_URL", "").strip()

        return cls(
            provider=provider,
            api_key=os.environ.get("VAULT_EMBEDDING_API_KEY"),
            base_url=base_url.rstrip("/") or None,
            model=model,
            profile_id=profile_id,
            embedding_dimensions=embedding_dimensions,
            timeout_seconds=_validate_embedding_timeout(
                _positive_float(
                    "VAULT_EMBEDDING_TIMEOUT_SECONDS",
                    os.environ.get(
                        "VAULT_EMBEDDING_TIMEOUT_SECONDS",
                        str(DEFAULT_EMBEDDING_TIMEOUT_SECONDS),
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class VaultSettings:
    enabled: bool
    database_url: str
    hss_database_url: str
    pool_size: int
    pool_timeout_seconds: int
    hss_pool_max_size: int
    process_count: int
    hss_connection_limit: int
    vault_connection_limit: int
    operational_connection_reserve: int
    text_search_config: str

    @classmethod
    def from_environment(cls) -> "VaultSettings":
        hss_database_url = os.environ.get("DATABASE_URL")
        if not hss_database_url:
            raise RuntimeError("DATABASE_URL is not set")

        enabled = vault_enabled()
        database_url = os.environ.get("VAULT_DATABASE_URL") or hss_database_url
        hss_limit = _positive_int(
            "DATABASE_CONNECTION_LIMIT",
            os.environ.get("DATABASE_CONNECTION_LIMIT", "20"),
        )
        vault_limit = _positive_int(
            "VAULT_DATABASE_CONNECTION_LIMIT",
            os.environ.get("VAULT_DATABASE_CONNECTION_LIMIT", str(hss_limit)),
        )

        return cls(
            enabled=enabled,
            database_url=normalize_sqlalchemy_url(database_url),
            hss_database_url=normalize_sqlalchemy_url(hss_database_url),
            pool_size=_positive_int(
                "VAULT_DB_POOL_SIZE",
                # 2 rather than 1 because a vault request checks out twice in
                # sequence -- once to authenticate, once to serve -- so at size
                # 1 a second concurrent request on the same worker waits out
                # pool_timeout_seconds and fails. One spare connection is what
                # makes concurrency possible at all, not a throughput tuning.
                os.environ.get("VAULT_DB_POOL_SIZE", "2"),
            ),
            pool_timeout_seconds=_positive_int(
                "VAULT_DB_POOL_TIMEOUT_SECONDS",
                os.environ.get("VAULT_DB_POOL_TIMEOUT_SECONDS", "5"),
            ),
            hss_pool_max_size=_positive_int(
                "HSS_DB_POOL_MAX_SIZE",
                # Must track app.db's default, which this only mirrors -- the
                # budget is validated here but spent there.
                os.environ.get("HSS_DB_POOL_MAX_SIZE", "4"),
            ),
            process_count=_positive_int(
                "HSS_PROCESS_COUNT",
                os.environ.get("HSS_PROCESS_COUNT", "2"),
            ),
            hss_connection_limit=hss_limit,
            vault_connection_limit=vault_limit,
            operational_connection_reserve=_positive_int(
                "DB_OPERATIONAL_CONNECTION_RESERVE",
                os.environ.get("DB_OPERATIONAL_CONNECTION_RESERVE", "2"),
            ),
            text_search_config=resolve_text_search_config(),
        )

    @property
    def shared_database(self) -> bool:
        return _database_target(self.database_url) == _database_target(
            self.hss_database_url
        )

    def validate_connection_budget(self) -> ConnectionBudget:
        hss_connections = self.hss_pool_max_size * self.process_count
        vault_connections = self.pool_size * self.process_count
        hss_required_unallocated = math.ceil(self.hss_connection_limit * 0.25)
        vault_required_unallocated = math.ceil(self.vault_connection_limit * 0.25)

        if self.shared_database:
            allocated = (
                hss_connections
                + vault_connections
                + self.operational_connection_reserve
            )
            available = self.hss_connection_limit - hss_required_unallocated
            if allocated > available:
                raise RuntimeError(
                    "Shared database connection budget exceeded: "
                    f"{allocated} allocated, {available} available after the "
                    "required 25% reserve"
                )
            return ConnectionBudget(
                shared_database=True,
                hss_allocated=hss_connections,
                vault_allocated=vault_connections,
                combined_allocated=allocated,
                hss_limit=self.hss_connection_limit,
                vault_limit=self.hss_connection_limit,
                hss_required_unallocated=hss_required_unallocated,
                vault_required_unallocated=hss_required_unallocated,
            )

        hss_allocated = hss_connections + self.operational_connection_reserve
        vault_allocated = vault_connections + self.operational_connection_reserve
        hss_available = self.hss_connection_limit - hss_required_unallocated
        vault_available = self.vault_connection_limit - vault_required_unallocated
        if hss_allocated > hss_available:
            raise RuntimeError(
                "HSS database connection budget exceeded: "
                f"{hss_allocated} allocated, {hss_available} available after "
                "the required 25% reserve"
            )
        if vault_allocated > vault_available:
            raise RuntimeError(
                "Vault database connection budget exceeded: "
                f"{vault_allocated} allocated, {vault_available} available after "
                "the required 25% reserve"
            )
        return ConnectionBudget(
            shared_database=False,
            hss_allocated=hss_allocated,
            vault_allocated=vault_allocated,
            combined_allocated=None,
            hss_limit=self.hss_connection_limit,
            vault_limit=self.vault_connection_limit,
            hss_required_unallocated=hss_required_unallocated,
            vault_required_unallocated=vault_required_unallocated,
        )
