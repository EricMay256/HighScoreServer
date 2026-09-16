"""The restore script: a dry run that writes nothing, then an apply that restores.

The service's restore is covered in ``test_human_deletes``. What is left to pin
here is the operator's half: that the preview is a preview, and that the exit
codes say what happened, so a dry run in a shell script stops before an apply
that would be refused.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.vault.auth import VaultScope
from app.vault.settings import vault_enabled
from app.vault.tables import vault_audit_events
from scripts.restore_human_note import OPERATOR_PRINCIPAL, restore
from tests.vault.test_human_deletes import (
    NOTES,
    _auth,
    _create,
    _credential,
    _delete,
    _history,
    _run,
)


pytestmark = pytest.mark.skipif(
    not vault_enabled(),
    reason="Vault routes are only registered when VAULT_ENABLED is true",
)

OPERATOR_SCOPES = (
    VaultScope.READ,
    VaultScope.HUMAN_READ,
    VaultScope.HUMAN_WRITE,
    VaultScope.HUMAN_DELETE,
)


def test_a_dry_run_writes_nothing_and_apply_restores(
    client: TestClient,
    configure_test_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for operator in _credential(OPERATOR_SCOPES):
        note = _create(client, operator, "scripted")
        assert _delete(client, operator, note["note_id"], base=1).status_code == 200

        try:
            assert asyncio.run(restore(note["note_id"], apply=False)) == 0
            assert "would restore" in capsys.readouterr().out
            assert _history(note["note_id"]) == [("create", 1), ("delete", 2)]

            assert asyncio.run(restore(note["note_id"], apply=True)) == 0
            assert _history(note["note_id"]) == [
                ("create", 1),
                ("delete", 2),
                ("restore", 3),
            ]
            fetched = client.get(
                f"{NOTES}/{note['note_id']}", headers=_auth(operator["token"])
            )
            assert fetched.status_code == 200

            capsys.readouterr()
            assert asyncio.run(restore(note["note_id"], apply=True)) == 1
            assert "not deleted" in capsys.readouterr().err
        finally:
            # The restore's audit event is the operator's, not the fixture
            # principal's, so the fixture's cleanup does not reach it.
            _run(
                lambda connection, note_id=note["note_id"]: connection.execute(
                    delete(vault_audit_events).where(
                        vault_audit_events.c.principal_id == OPERATOR_PRINCIPAL,
                        vault_audit_events.c.target_id == note_id,
                    )
                )
            )
