"""Alembic environment.

Raw-SQL migrations: there are no SQLAlchemy ORM models and autogenerate is
intentionally unused, so target_metadata is None. The database URL is read
from the DATABASE_URL environment variable at runtime (never committed).
"""
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from pathlib import Path
from dotenv import load_dotenv

# Load .env from the repo root so Alembic sees the same DATABASE_URL the app
# uses, with no per-shell `$env:` setup. override=False is deliberate: a
# DATABASE_URL already present in the process environment (e.g. exported from
# `heroku config:get` for a prod stamp, or pointed at the step-6 throwaway DB)
# WINS over .env — so explicit operations can't accidentally hit the dev DB.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No models to diff against — these migrations are hand-written raw SQL.
target_metadata = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Heroku issues 'postgres://', which SQLAlchemy 1.4+/2.0 no longer accepts.
    # Normalize the scheme and pin the driver explicitly so behavior doesn't
    # depend on SQLAlchemy's default-dialect resolution. The driver is psycopg
    # (psycopg3) — migrations run synchronously through SQLAlchemy's sync
    # psycopg dialect; there is no async/asyncpg path here (see ADR 0014).
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def run_migrations_offline() -> None:
    """Render migrations as SQL without connecting (alembic ... --sql)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()