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

[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Format
)

$ErrorActionPreference = "Stop"

# CI scope. Keep in sync with .github/workflows/ci.yml ruff step.
$Targets = @("app/", "tests/", "scripts/")

if ($Format) {
    Write-Host "→ Running ruff format" -ForegroundColor Cyan
    ruff format @Targets
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Fix) {
    Write-Host "→ Running ruff check --fix" -ForegroundColor Cyan
    ruff check @Targets --fix
    # Don't exit on this — the verify step below is the gate.
}

Write-Host "→ Running ruff check (verify)" -ForegroundColor Cyan
ruff check @Targets
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ Lint failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "✓ Lint clean" -ForegroundColor Green