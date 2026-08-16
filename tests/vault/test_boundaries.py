import ast
from pathlib import Path


VAULT_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "vault"
HOST_PACKAGE = VAULT_PACKAGE.parent
HOST_VAULT_IMPORT_ALLOWLIST = {HOST_PACKAGE / "main.py"}

# rglob, not glob: the package is flat today, so the two are equivalent and the
# distinction looks academic. It stops being academic the moment anyone adds a
# subpackage, at which point a non-recursive walk would quietly stop enforcing
# every rule below on the newest code — the failure mode being silence.
FORBIDDEN_IMPORT_PREFIXES = (
    "app.auth",
    "app.auth_identities",
    "app.auth_routes",
    "app.db",
    "app.leaderboard_routes",
    "app.models",
    "app.periods",
    "app.view_routes",
)
FORBIDDEN_SQLALCHEMY_NAMES = {
    "DeclarativeBase",
    "Session",
    "async_sessionmaker",
    "declarative_base",
    "mapped_column",
    "relationship",
    "sessionmaker",
}


def imported_names(tree: ast.AST) -> list[tuple[str, set[str]]]:
    imports: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, set()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                (
                    node.module or "",
                    {alias.name for alias in node.names},
                )
            )
    return imports


def test_vault_package_has_no_leaderboard_domain_imports() -> None:
    violations: list[str] = []
    for path in VAULT_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, _names in imported_names(tree):
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{path.name}: {module}")

    assert violations == []


def test_host_modules_do_not_import_vault_internals() -> None:
    """Only the composition root may know the staged vault package exists."""

    violations: list[str] = []
    for path in HOST_PACKAGE.rglob("*.py"):
        if VAULT_PACKAGE in path.parents or path in HOST_VAULT_IMPORT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, _names in imported_names(tree):
            if module == "app.vault" or module.startswith("app.vault."):
                violations.append(f"{path.relative_to(HOST_PACKAGE)}: {module}")

    assert violations == []


def test_vault_package_imports_siblings_relatively() -> None:
    # Extraction must be a directory move, not a find-and-replace. An absolute
    # self-reference is what turns the former into the latter.
    violations: list[str] = []
    for path in VAULT_PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations.extend(
                    f"{path.name}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name == "app" or alias.name.startswith("app.")
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module or ""
                if module == "app" or module.startswith("app."):
                    violations.append(f"{path.name}:{node.lineno}: from {module}")

    assert violations == []


def test_vault_package_does_not_introduce_sqlalchemy_orm() -> None:
    violations: list[str] = []
    for path in VAULT_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, names in imported_names(tree):
            forbidden_names = names & FORBIDDEN_SQLALCHEMY_NAMES
            if module.startswith("sqlalchemy.orm") or forbidden_names:
                violations.append(f"{path.name}: {module} {sorted(forbidden_names)}")

    assert violations == []


def test_vault_package_has_no_create_all_call() -> None:
    violations: list[str] = []
    for path in VAULT_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_all"
            ):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []
