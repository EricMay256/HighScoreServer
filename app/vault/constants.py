"""Stable persistence contracts shared across the vault package."""

import os
import re


# Changing this value requires a new Alembic column/index migration and a
# controlled re-embedding job. Historical migrations intentionally keep their
# literal dimension so changing application code cannot rewrite old DDL.
EMBEDDING_DIMENSIONS = 1536

DEFAULT_TEXT_SEARCH_CONFIG = "english"

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
