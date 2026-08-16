"""Dedicated Alembic environment for the vault schema.

This lineage owns only ``vault.*`` objects and stores its revision in
``vault.vault_alembic_version``. It uses VAULT_DATABASE_URL when set and falls
back to DATABASE_URL for the initial shared-database topology.
"""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool, text

from app.vault.settings import normalize_sqlalchemy_url


VAULT_SCHEMA = "vault"
VERSION_TABLE = "vault_alembic_version"

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which switches off every
    # logger created before Alembic ran — including the application's. Harmless
    # for the CLI, which exits, but destructive when migrations run in-process
    # and the process keeps going, as the test suite does.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Migrations remain explicit and reviewed. Core metadata is checked separately
# by schema-drift tests rather than used as an application migration path.
target_metadata = None


def _database_url() -> str:
    url = os.environ.get("VAULT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("VAULT_DATABASE_URL and DATABASE_URL are both unset")
    return normalize_sqlalchemy_url(url)


def _configure_context(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        version_table_schema=VAULT_SCHEMA,
        include_schemas=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure_context(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    context.execute(f"CREATE SCHEMA IF NOT EXISTS {VAULT_SCHEMA}")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Alembic creates the version table before it invokes the first
        # revision, so its containing schema must be bootstrapped here. No
        # application table, type, or extension is created outside revisions.
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VAULT_SCHEMA}"))
        connection.commit()
        _configure_context(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
