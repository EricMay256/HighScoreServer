"""What an operator is shown before a script writes to a live database.

Untested until now, which is how it came to print `None`: the function was
copy-pasted into three scripts and never exercised anywhere. Its whole job is
to stop somebody running `--apply` against the wrong target, so the rendering
being unambiguous *is* the feature, not presentation.

Pure and database-free -- these are string assertions about a URL parser.
"""

import pytest

from app.vault.db import describe_database


HEROKU = "postgresql://u:secret@ec2-1-2-3.compute.amazonaws.com:5432/d7abc"
LOCAL = "postgresql://postgres:secret@localhost:5432/leaderboard"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (HEROKU, "ec2-1-2-3.compute.amazonaws.com:5432/d7abc"),
        (LOCAL, "localhost:5432/leaderboard"),
        # No port stated. Rendered without one rather than with an invented
        # 5432: the URL did not say, so neither does this.
        ("postgresql://u:pw@localhost/leaderboard", "localhost/leaderboard"),
        # A socket connection has no host at all.
        ("postgresql:///leaderboard", "(local socket)/leaderboard"),
        # Used to render "host:5432/None", which reads as a database actually
        # called None rather than as one the URL omitted.
        ("postgresql://u:pw@host:5432", "host:5432/(no database)"),
        # ...and the trailing-slash spelling of the same omission, which used
        # to render as a bare "host:5432/" and look like a truncated string.
        ("postgresql://u:pw@host:5432/", "host:5432/(no database)"),
        # Bracketed, or "::1:5432/db" leaves a reader guessing where the
        # address stops and the port begins.
        ("postgresql://u:pw@[::1]:5432/dbname", "[::1]:5432/dbname"),
        ("postgresql://u:pw@[2001:db8::1]/dbname", "[2001:db8::1]/dbname"),
    ],
)
def test_the_target_renders_unambiguously(url: str, expected: str) -> None:
    assert describe_database(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        HEROKU,
        LOCAL,
        "postgresql://u:pw@[::1]:5432/dbname",
        "postgresql://u:p%40ss%3Aword@host:5432/db",
    ],
)
def test_no_credential_ever_reaches_the_output(url: str) -> None:
    """The property that matters most, because this string gets pasted.

    Asserted over the password *and* the username: a Heroku URL carries both,
    and operators paste this line into issues and chat without rereading it.
    """

    rendered = describe_database(url)
    for secret in ("secret", "pw", "p%40ss", "u:", "postgres:"):
        assert secret not in rendered, f"{secret!r} leaked into {rendered!r}"
