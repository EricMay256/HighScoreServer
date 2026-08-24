"""The operator-password hashing script.

Exercised here rather than by hand because ``getpass`` reads the console
directly on Windows -- it does not read piped stdin -- so a shell check either
hangs or silently tests something else. Stubbing the prompt is the only way to
drive all three paths, and it is what makes the "does not echo" property
testable at all.

No database. This script touches none.
"""

import asyncio
import sys

import pytest

from app.vault.passwords import MAX_PASSWORD_BYTES, verify_password
from scripts import hash_vault_operator_password as script


PASSWORD = "correct horse battery staple"


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch):
    """Feed ``getpass`` a queue of answers and record what it was asked."""

    asked: list[str] = []

    def install(*answers: str) -> list[str]:
        queue = list(answers)

        def fake_getpass(prompt: str = "") -> str:
            asked.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr(script.getpass, "getpass", fake_getpass)
        return asked

    return install


def run(capsys) -> tuple[int, str, str]:
    code = script.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_confirmed_password_prints_a_verifiable_bcrypt_hash(
    prompts, capsys
) -> None:
    """The one thing that actually matters: the output verifies the input.

    A runbook that produced a hash the login could not check would fail only at
    the login form, after a deploy, with a message saying just "wrong password".
    """

    prompts(PASSWORD, PASSWORD)

    code, out, _ = run(capsys)
    hashed = out.strip().splitlines()[-1].strip()

    assert code == 0
    assert hashed.startswith("$2")
    assert asyncio.run(verify_password(PASSWORD, hashed)) is True


def test_the_password_is_never_printed(prompts, capsys) -> None:
    """``getpass`` keeps it off the terminal; this keeps it out of the output.

    The failure this guards is a well-meant "echo what you entered so you can
    check it", which would put the secret in a scrollback and in any CI log
    that ran the script.
    """

    prompts(PASSWORD, PASSWORD)

    _, out, err = run(capsys)

    assert PASSWORD not in out
    assert PASSWORD not in err


def test_it_asks_twice_and_refuses_a_mismatch(prompts, capsys) -> None:
    """A typo in a write-only field is otherwise found after a deploy."""

    asked = prompts(PASSWORD, PASSWORD + "!")

    code, out, err = run(capsys)

    assert len(asked) == 2
    assert code == 1
    assert "do not match" in err
    assert "$2" not in out


def test_an_empty_password_is_refused_without_a_second_prompt(
    prompts, capsys
) -> None:
    asked = prompts("", "")

    code, _, err = run(capsys)

    assert len(asked) == 1
    assert code == 1
    assert "Empty password" in err


def test_an_over_long_password_explains_the_truncation_rather_than_hashing(
    prompts, capsys
) -> None:
    """bcrypt would ignore everything past 72 bytes, so a different passphrase
    sharing the prefix would verify. The operator hears about it once, here."""

    too_long = "a" * (MAX_PASSWORD_BYTES + 1)
    prompts(too_long, too_long)

    code, out, err = run(capsys)

    assert code == 1
    assert str(MAX_PASSWORD_BYTES) in err
    assert "$2" not in out


def test_a_closed_stdin_exits_without_a_traceback(prompts, capsys) -> None:
    """Piped input, or an operator changing their mind at the prompt."""

    prompts()

    code, out, _ = run(capsys)

    assert code == 1
    assert "$2" not in out


def test_the_script_has_no_module_level_side_effects() -> None:
    """The host's ``AGENTS.md`` rule: importing must not run anything.

    This module has already been imported at the top of this file, so reaching
    here at all is most of the assertion; the rest is that it did not need a
    database URL to do it.
    """

    assert script.__name__ in sys.modules
    assert callable(script.main)
