"""Vault configuration and database connection-budget validation."""

from dataclasses import dataclass
import math
import os

from sqlalchemy.engine import make_url


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

    @classmethod
    def from_environment(cls) -> "VaultSettings":
        hss_database_url = os.environ.get("DATABASE_URL")
        if not hss_database_url:
            raise RuntimeError("DATABASE_URL is not set")

        enabled = _parse_bool(os.environ.get("VAULT_ENABLED", "false"))
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
                os.environ.get("VAULT_DB_POOL_SIZE", "1"),
            ),
            pool_timeout_seconds=_positive_int(
                "VAULT_DB_POOL_TIMEOUT_SECONDS",
                os.environ.get("VAULT_DB_POOL_TIMEOUT_SECONDS", "5"),
            ),
            hss_pool_max_size=_positive_int(
                "HSS_DB_POOL_MAX_SIZE",
                os.environ.get("HSS_DB_POOL_MAX_SIZE", "5"),
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
