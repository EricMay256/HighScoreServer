# Lint the Python source matching the CI scope.
#
# Usage:
#   .\scripts\lint.ps1            # check only (matches CI exactly)
#   .\scripts\lint.ps1 -Fix       # auto-fix safe issues, then re-check
#   .\scripts\lint.ps1 -Format    # also run ruff format
#   .\scripts\lint.ps1 -Fix -Format
#
# The check-only mode is the one to run before committing — it mirrors
# what CI will do, no surprises. The -Fix mode is what to run while
# actively writing code.
#
# Bash/WSL equivalent: scripts/lint.sh. Keep the two in sync.

[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Format
)

$ErrorActionPreference = "Stop"

# CI scope. Keep in sync with .github/workflows/ci.yml ruff step.
#
# The whole tree, not a path list. The list used to be app/ tests/ scripts/,
# which left migrations/, run_dev.py and wsgi.py unlinted — nine findings CI
# could not see. A list that must be extended whenever a directory is added
# drifts silently; "." cannot. Ruff honours .gitignore, so .venv/ and the
# worktrees under .claude/ are skipped.
$CheckTargets = @(".")

# Formatting stays narrow on purpose. `ruff format` rewrites whole files, and
# migrations/ is reviewed historical SQL that should not be reformatted
# wholesale by anyone who passes -Format while working on something else.
$FormatTargets = @("app/", "tests/", "scripts/")

if ($Format) {
    Write-Host "→ Running ruff format" -ForegroundColor Cyan
    ruff format @FormatTargets
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Fix) {
    Write-Host "→ Running ruff check --fix" -ForegroundColor Cyan
    ruff check @CheckTargets --fix
    # Don't exit on this — the verify step below is the gate.
}

Write-Host "→ Running ruff check (verify)" -ForegroundColor Cyan
ruff check @CheckTargets
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ Lint failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "✓ Lint clean" -ForegroundColor Green