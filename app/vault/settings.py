"""Vault configuration and database connection-budget validation."""

import math
import os
import re
from dataclasses import dataclass

from sqlalchemy.engine import make_url

from .constants import (
    DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
    EMBEDDING_DIMENSIONS,
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


def vault_enabled() -> bool:
    """Whether the vault runtime is switched on for this process.

    Separate from ``VaultSettings.from_environment`` so route registration can
    ask the question without requiring a database URL to be present.
    """

    return _parse_bool(os.environ.get("VAULT_ENABLED", "false"))


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
            timeout_seconds=_positive_float(
                "VAULT_EMBEDDING_TIMEOUT_SECONDS",
                os.environ.get(
                    "VAULT_EMBEDDING_TIMEOUT_SECONDS",
                    str(DEFAULT_EMBEDDING_TIMEOUT_SECONDS),
                ),
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
