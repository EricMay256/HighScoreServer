"""Dependency-light helpers historical vault revisions may import safely."""

import os
import re


DEFAULT_TEXT_SEARCH_CONFIG = "english"
_TEXT_SEARCH_CONFIG_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def resolve_text_search_config() -> str:
    """Resolve the identifier-safe configuration interpolated into migration DDL."""

    value = os.environ.get(
        "VAULT_TEXT_SEARCH_CONFIG", DEFAULT_TEXT_SEARCH_CONFIG
    ).strip()
    if not _TEXT_SEARCH_CONFIG_PATTERN.fullmatch(value):
        raise RuntimeError(
            "VAULT_TEXT_SEARCH_CONFIG must match ^[a-z_][a-z0-9_]*$; "
            f"got {value!r}"
        )
    return value
