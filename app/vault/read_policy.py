"""Which vault paths the read surface may serve.

The governance source of truth is ``ai_read`` in ``folders.yml``, which lives in
the private knowledge-platform repository. Excluded folders are not imported at
all, so ordinarily there is no row here to withhold. This module is the second
layer: it re-states the readable set so a row that *did* land — imported while a
folder was readable and reclassified afterwards, or written by a future path
that skipped the importer — is still not served.

Two layers, because the failure modes differ. Not importing protects against
every query, every log line, and every future endpoint at once. Filtering at
query time protects against the window where the corpus is ahead of the policy.
Neither subsumes the other.

**Fail closed.** A path matching no prefix is unreadable. `folders.yml` defaults
`ai_read` to ``forbidden`` for the same reason: a folder added later must not
become agent-readable merely because nobody remembered to exclude it.

Not configuration, for the reason ADR 0008 gives about ``READABLE_STATUSES``: a
deployment must not be able to opt into serving content the governance layer
excluded. Changing what is readable is a governance change that travels through
`folders.yml`, this constant, and a reconciliation run together.
"""

from sqlalchemy import ColumnElement, false, or_

from .tables import vault_documents


# Mirrors every folders.yml rule carrying `ai_read: allowed`, as literal path
# prefixes. Every glob in that file is a literal prefix followed by `/**`, which
# is what makes this a prefix test rather than a glob engine (see ADR 0010).
#
# Order is irrelevant — this is a union, not a precedence chain. `folders.yml`
# resolves precedence before a path ever reaches here, and a deeper `forbidden`
# rule under a readable parent must therefore be re-stated as a hole below.
READABLE_PATH_PREFIXES: tuple[str, ...] = (
    # Agents must be able to read the rules they are asked to follow.
    "00 Governance/",
    # Agent-authored, but enumerated rather than a blanket "Agent/". folders.yml
    # has no `Agent/**` catch-all, so a subfolder nobody has classified falls
    # through to `default: ai_read: forbidden`. A broad prefix here would admit
    # it instead -- failing open exactly where the governance layer fails
    # closed, which is the guarantee this module exists to keep.
    "Agent/Promotion Candidates/",
    "Agent/notes/",
    "Agent/review/",
    "Agent/wiki/",
    # The agent's own proposals into the human inbox.
    "Human/01 Inbox/AI/",
    "Human/03 Projects/",
    "Human/04 Areas/",
    "Human/05 Decisions/",
    "Human/06 Reference/",
    "Human/08 Resources/",
    "Human/09 Systems/",
    "Human/17 Concepts/",
)

# Prefixes that sit *inside* a readable prefix but are excluded. Empty today:
# no `ai_read: forbidden` rule currently nests under an `allowed` one. It exists
# because the union above cannot express that on its own, and discovering the
# need at the moment someone adds such a rule is discovering it too late.
EXCLUDED_PATH_PREFIXES: tuple[str, ...] = ()


def is_readable_path(vault_path: str) -> bool:
    """Whether a vault path may be served to an agent."""

    if any(vault_path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
        return False
    return any(vault_path.startswith(prefix) for prefix in READABLE_PATH_PREFIXES)


def readable_path_predicate() -> ColumnElement[bool]:
    """The same rule as SQL, for filtering inside a query.

    ``startswith`` compiles to ``LIKE 'prefix%'`` with the literal escaped,
    which the ``text_pattern_ops`` index from ADR 0010 can serve.
    """

    if not READABLE_PATH_PREFIXES:
        # Nothing readable: refuse everything rather than degrade to no filter.
        return false()

    allowed = or_(
        *(
            vault_documents.c.vault_path.startswith(prefix, autoescape=True)
            for prefix in READABLE_PATH_PREFIXES
        )
    )
    if not EXCLUDED_PATH_PREFIXES:
        return allowed
    return allowed & ~or_(
        *(
            vault_documents.c.vault_path.startswith(prefix, autoescape=True)
            for prefix in EXCLUDED_PATH_PREFIXES
        )
    )
