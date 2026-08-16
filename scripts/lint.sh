#!/usr/bin/env bash
# Lint the Python source matching the CI scope.
#
# Usage:
#   ./scripts/lint.sh              # check only (matches CI exactly)
#   ./scripts/lint.sh --fix        # auto-fix safe issues, then re-check
#   ./scripts/lint.sh --format     # also run ruff format
#   ./scripts/lint.sh --fix --format
#
# The check-only mode is the one to run before committing — it mirrors
# what CI will do, no surprises. The --fix mode is what to run while
# actively writing code.
#
# PowerShell equivalent: scripts/lint.ps1. Keep the two in sync.

set -euo pipefail

FIX=0
FORMAT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --fix)    FIX=1 ;;
        --format) FORMAT=1 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

# CI scope. Keep in sync with .github/workflows/ci.yml ruff step.
TARGETS=(app/ tests/ scripts/)

CYAN=$'\033[36m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'

if [ "$FORMAT" -eq 1 ]; then
    echo "${CYAN}→ Running ruff format${RESET}"
    ruff format "${TARGETS[@]}"
fi

if [ "$FIX" -eq 1 ]; then
    echo "${CYAN}→ Running ruff check --fix${RESET}"
    # Don't exit on this — the verify step below is the gate.
    ruff check "${TARGETS[@]}" --fix || true
fi

echo "${CYAN}→ Running ruff check (verify)${RESET}"
if ! ruff check "${TARGETS[@]}"; then
    echo "${RED}✗ Lint failed${RESET}"
    exit 1
fi

echo "${GREEN}✓ Lint clean${RESET}"
