"""Helpers for migration-topology tests against disposable databases."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from app.vault.settings import normalize_sqlalchemy_url


REPO_ROOT = Path(__file__).resolve().parents[2]


def _psycopg_url(url: str) -> str:
    return normalize_sqlalchemy_url(url).replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )


def database_url(base_url: str, database_name: str) -> str:
    parsed = make_url(normalize_sqlalchemy_url(base_url)).set(database=database_name)
    return _psycopg_url(parsed.render_as_string(hide_password=False))


def create_database(base_url: str, prefix: str) -> tuple[str, str]:
    database_name = f"{prefix}_{uuid4().hex[:12]}"
    with psycopg.connect(_psycopg_url(base_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    return database_name, database_url(base_url, database_name)


def drop_database(base_url: str, database_name: str) -> None:
    with psycopg.connect(_psycopg_url(base_url), autocommit=True) as connection:
        connection.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (database_name,),
        )
        connection.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
        )


@contextmanager
def migration_environment(
    *,
    database_url_value: str,
    vault_database_url_value: str | None = None,
) -> Iterator[None]:
    old_database_url = os.environ.get("DATABASE_URL")
    old_vault_database_url = os.environ.get("VAULT_DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url_value
    if vault_database_url_value is None:
        os.environ.pop("VAULT_DATABASE_URL", None)
    else:
        os.environ["VAULT_DATABASE_URL"] = vault_database_url_value
    try:
        yield
    finally:
        if old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_database_url
        if old_vault_database_url is None:
            os.environ.pop("VAULT_DATABASE_URL", None)
        else:
            os.environ["VAULT_DATABASE_URL"] = old_vault_database_url


def run_leaderboard_migration(database_url_value: str, revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    with migration_environment(database_url_value=database_url_value):
        command.upgrade(config, revision) if revision != "base" else command.downgrade(
            config, revision
        )


def run_vault_migration(database_url_value: str, revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic-vault.ini"))
    with migration_environment(
        database_url_value=database_url_value,
        vault_database_url_value=database_url_value,
    ):
        command.upgrade(config, revision) if revision != "base" else command.downgrade(
            config, revision
        )
