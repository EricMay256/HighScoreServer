import os
import subprocess
from pathlib import Path

import pytest


def find_bash() -> str | None:
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            git_bash = Path(program_files) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
        return None

    return "bash"


pytestmark = pytest.mark.skipif(find_bash() is None, reason="Bash is not installed")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_release_script_runs_vault_migrations_for_true_values(
    tmp_path: Path,
    value: str,
) -> None:
    result, calls = run_release_script(tmp_path, value)

    assert result.returncode == 0, result.stderr
    assert calls == ["upgrade head", "-c alembic-vault.ini upgrade head"]


@pytest.mark.parametrize("value", [None, "0", "false", "FALSE", "no", "off", " Off "])
def test_release_script_skips_vault_migrations_for_false_values(
    tmp_path: Path,
    value: str | None,
) -> None:
    result, calls = run_release_script(tmp_path, value)

    assert result.returncode == 0, result.stderr
    assert calls == ["upgrade head"]


@pytest.mark.parametrize("value", ["", "enabled", "2"])
def test_release_script_rejects_invalid_boolean_values(
    tmp_path: Path,
    value: str,
) -> None:
    result, calls = run_release_script(tmp_path, value)

    assert result.returncode != 0
    assert "Invalid boolean value for VAULT_ENABLED" in result.stderr
    assert calls == ["upgrade head"]


def run_release_script(
    tmp_path: Path,
    value: str | None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash = find_bash()
    assert bash is not None

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_path = tmp_path / "alembic-calls.txt"
    fake_alembic = bin_dir / "alembic"
    fake_alembic.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$ALEMBIC_CALLS_PATH"\n',
        encoding="utf-8",
    )
    fake_alembic.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["ALEMBIC_CALLS_PATH"] = str(calls_path)
    if value is None:
        environment.pop("VAULT_ENABLED", None)
    else:
        environment["VAULT_ENABLED"] = value

    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [bash, str(repository_root / "scripts" / "release.sh")],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    return result, calls
