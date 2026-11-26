"""
Checks that ``folders.yml`` and ``app/vault/read_policy.py`` still agree.

``ai_read`` in ``folders.yml`` is the governance source of truth, and
``READABLE_PATH_PREFIXES`` re-states it in code as the second layer that
withholds a row the importer should never have admitted (see the module
docstring there, and vault ADR 0010). Two statements of one rule drift, and
``read_policy.py`` says changing what is readable travels through ``folders.yml``
and that constant together. Nothing enforced the "together" part: the unit tests
transcribe the expected answers by hand, so they agree with whoever last edited
them rather than with the file.

This closes that. It reads the real ``folders.yml`` and diffs it against the
constants, which is the verification vault ADR 0048's phase A asks for before
any note is enrolled.

**The two directions are not equally bad.** A prefix governance allows and the
code omits is over-restrictive: content is withheld that policy would serve --
stale, visible, safe. A prefix the code allows and governance does not is
failing open, which is the failure this module exists to prevent and the reason
the exit code does not distinguish them: both mean the pair is unverified.

Not a pytest case, deliberately. ``folders.yml`` lives in the private
knowledge-platform repository and is absent in CI, so a test over it would be
skipped exactly where tests are supposed to run. This is an operator check with
the path supplied, run when either side changes.

Reads two files. Touches no database, makes no API calls, writes nothing.

Usage:
    python -m scripts.check_read_policy_parity \
        --folders-yml "../../knowledge-platform/Vault/00 Governance/Schemas/folders.yml"

Exit status:
    0   the two agree
    1   they disagree, or the file could not be read
"""

import argparse
import sys
from pathlib import Path

import yaml

from app.vault.read_policy import EXCLUDED_PATH_PREFIXES, READABLE_PATH_PREFIXES


# Every rule in folders.yml is a literal prefix followed by this suffix, which
# is what lets read_policy.py be a prefix test rather than a glob engine.
GLOB_SUFFIX = "/**"


def prefix_for_glob(glob: str) -> str | None:
    """The literal path prefix a folders.yml glob selects, if it has one.

    Returns ``None`` for a pattern this check cannot reduce -- a wildcard in the
    middle, say -- so the caller can report it rather than silently skip it.
    """

    if not glob.endswith(GLOB_SUFFIX):
        return None
    literal = glob[: -len(GLOB_SUFFIX)]
    if "*" in literal or "?" in literal:
        return None
    return literal + "/"


def governance_prefixes(document: dict[str, object]) -> tuple[set[str], set[str], list[str]]:
    """Split the folders.yml rules into readable, forbidden, and unreducible."""

    folders = document.get("folders")
    if not isinstance(folders, dict):
        raise ValueError("folders.yml has no `folders:` mapping")

    readable: set[str] = set()
    forbidden: set[str] = set()
    unreducible: list[str] = []

    for glob, rule in folders.items():
        if not isinstance(rule, dict):
            raise ValueError(f"rule for {glob!r} is not a mapping")
        prefix = prefix_for_glob(str(glob))
        if prefix is None:
            unreducible.append(str(glob))
            continue
        # Absent means inherited from `default:`, which is checked separately.
        # Only an explicit `allowed` puts a prefix in the readable set.
        if rule.get("ai_read") == "allowed":
            readable.add(prefix)
        elif rule.get("ai_read") == "forbidden":
            forbidden.add(prefix)

    return readable, forbidden, unreducible


def nested_forbidden_prefixes(readable: set[str], forbidden: set[str]) -> set[str]:
    """Forbidden prefixes sitting inside a readable one.

    These are the only ones ``EXCLUDED_PATH_PREFIXES`` needs to carry: a union of
    prefixes cannot express a hole, and one outside every readable prefix is
    already covered by failing closed.
    """

    return {
        candidate
        for candidate in forbidden
        if any(
            candidate.startswith(parent) and candidate != parent for parent in readable
        )
    }


def report(folders_yml: Path) -> int:
    document = yaml.safe_load(folders_yml.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("folders.yml did not parse as a mapping")

    readable, forbidden, unreducible = governance_prefixes(document)
    declared = set(READABLE_PATH_PREFIXES)

    print(f"governance : {folders_yml}")
    print("runtime    : app/vault/read_policy.py")
    print(f"\nai_read allowed in folders.yml : {len(readable)}")
    print(f"READABLE_PATH_PREFIXES         : {len(declared)}")

    failures: list[str] = []

    missing = sorted(readable - declared)
    if missing:
        failures.append("governance allows prefixes the runtime withholds")
        print("\nGovernance allows, runtime does not serve (over-restrictive):")
        for prefix in missing:
            print(f"  {prefix}")

    extra = sorted(declared - readable)
    if extra:
        failures.append("the runtime serves prefixes governance does not allow")
        print("\nRuntime serves, governance does not allow (FAILS OPEN):")
        for prefix in extra:
            print(f"  {prefix}")

    default = document.get("default")
    default_read = default.get("ai_read") if isinstance(default, dict) else None
    if default_read != "forbidden":
        failures.append(f"`default: ai_read` is {default_read!r}, not 'forbidden'")
        print(
            f"\n`default:` carries ai_read {default_read!r}. The prefix union assumes"
            "\nan unclassified folder is forbidden; it no longer is."
        )

    nested = nested_forbidden_prefixes(readable, forbidden)
    declared_exclusions = set(EXCLUDED_PATH_PREFIXES)
    uncovered = sorted(nested - declared_exclusions)
    if uncovered:
        failures.append("a forbidden folder nests inside a readable one and is not excluded")
        print("\nForbidden inside a readable prefix, missing from EXCLUDED_PATH_PREFIXES:")
        for prefix in uncovered:
            print(f"  {prefix}")

    stranded = sorted(declared_exclusions - nested)
    if stranded:
        failures.append("EXCLUDED_PATH_PREFIXES carries a prefix nothing nests under")
        print("\nEXCLUDED_PATH_PREFIXES entries that are not inside any readable prefix:")
        for prefix in stranded:
            print(f"  {prefix}")

    if unreducible:
        failures.append("a rule could not be reduced to a literal prefix")
        print("\nRules this check cannot reduce to a prefix -- verify by hand:")
        for glob in unreducible:
            print(f"  {glob}")

    if failures:
        print(f"\nDISAGREE -- {len(failures)} finding(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAGREE. Every ai_read: allowed rule is declared, nothing extra is served,")
    print("the default is fail-closed, and no forbidden folder nests inside a readable one.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff folders.yml's ai_read rules against read_policy.py.",
    )
    parser.add_argument(
        "--folders-yml",
        required=True,
        type=Path,
        help="Path to the governance folders.yml. Required and never defaulted: "
        "it lives in a private repository whose location is not this one's to "
        "assume.",
    )
    arguments = parser.parse_args()

    if not arguments.folders_yml.is_file():
        print(f"No such file: {arguments.folders_yml}", file=sys.stderr)
        return 1

    try:
        return report(arguments.folders_yml)
    except (ValueError, yaml.YAMLError) as error:
        print(f"Could not read the governance file: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
