"""The vault's own Jinja2 environment.

**Not HSS's.** The host renders Jinja2 views from a root-level ``templates/``
directory, and reaching for its ``base.html`` would be exactly the commingling
the extraction manifest exists to prevent: ``app/vault/`` moves as a directory,
and a page depending on a host asset does not move with it. Worse, the boundary
test scans *imports* -- a ``{% extends "base.html" %}`` would pass every guard
in the repository and fail only at extraction, months later, as a missing file.

So the vault carries ``app/vault/templates/`` and builds its own environment,
the same way ``rate_limit.py`` builds its own ``Limiter`` rather than sharing
the host's. ``jinja2`` is a shared third-party dependency, not a host import;
the package still contains no ``from app.``.

**Autoescaping is on and not optional.** The one page this renders is a login
form that takes a password and displays a client name chosen by whoever
registered -- registration is open, so that string is attacker-controlled. A
template engine was chosen over building one page's HTML by hand precisely
because that is where escaping mistakes turn into injected markup on a form
that takes a password.
"""

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"


@lru_cache(maxsize=1)
def vault_templates() -> Environment:
    """The environment, built once per process.

    Cached rather than module-level so importing this module has no side
    effect and touches no filesystem -- the host's ``AGENTS.md`` rule, and the
    reason Alembic can import the package without a template directory being
    present.

    ``autoescape`` is selected by filename rather than passed as ``True`` so
    that a future non-markup template (a plain-text email, say) is not escaped
    into nonsense, while every ``.html`` is escaped whatever it holds.
    """

    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIRECTORY),
        autoescape=select_autoescape(default_for_string=True),
        # Keeps the rendered page free of the blank lines that block tags would
        # otherwise leave. Cosmetic, and cheap to set once.
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(template_name: str, **context: object) -> str:
    """Render one vault template to a string."""

    return vault_templates().get_template(template_name).render(**context)
