"""The application must not depend on the working directory it starts in.

Host ADR 0016 composes this app from another repository's process
(knowledge-vault, vault ADR 0062), whose working directory is not this checkout.
``StaticFiles(directory="public")`` and ``Jinja2Templates(directory="templates")``
resolved against the working directory and failed there at import; both now
resolve from the package. Building the app from an unrelated directory is the
regression check.
"""

import os
import subprocess
import sys
from pathlib import Path


def test_the_app_builds_from_an_unrelated_working_directory(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root)
    environment["VAULT_ENABLED"] = "false"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.main, app.view_routes; "
                "from pathlib import Path; "
                "assert Path(app.main._PUBLIC_DIR).is_dir(); "
                "assert app.view_routes.templates.get_template('base.html')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
