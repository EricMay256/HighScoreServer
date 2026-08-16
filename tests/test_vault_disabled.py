import os
import subprocess
import sys
from pathlib import Path


def test_disabled_vault_does_not_import_or_parse_dormant_configuration() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "VAULT_ENABLED": "false",
            "VAULT_DB_POOL_SIZE": "not-an-integer",
            "VAULT_DB_POOL_TIMEOUT_SECONDS": "invalid",
            "VAULT_RATE_LIMIT_STORAGE_URI": "://invalid",
            "VAULT_PREAUTH_RATE_LIMIT": "not-a-limit",
            "VAULT_EMBEDDING_TIMEOUT_SECONDS": "forever",
        }
    )

    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from unittest.mock import AsyncMock, patch; "
                "from fastapi.testclient import TestClient; "
                "import app.main; "
                "stack = ["
                "patch.object(app.main, 'init_db', new_callable=AsyncMock), "
                "patch.object(app.main, 'close_db', new_callable=AsyncMock), "
                "patch.object(app.main, 'init_cache'), "
                "patch.object(app.main, 'close_cache', new_callable=AsyncMock)]; "
                "[item.start() for item in stack]; "
                "client = TestClient(app.main.app); client.__enter__(); client.__exit__(None, None, None); "
                "assert 'app.vault.routes' not in sys.modules; "
                "assert 'app.vault.db' not in sys.modules; "
                "assert 'app.vault.rate_limit' not in sys.modules"
            ),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
