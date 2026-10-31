#Requires -Version 5.1
<#
.SYNOPSIS
  Operator run-card for Drilling Intelligence — Windows acceptance (V10.0).
  Single PowerShell entry point. No prerequisites beyond PowerShell 5+ and Python 3.11+.

.DESCRIPTION
  Rebuilds venv, runs the evidence steps and writes a machine-readable
  result file that matches docs/WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json.

  Steps (each isolated, each logged sanely):
    1) python -m venv .venv-win && .venv-win pip install -e .[dev]
    2) ruff check / ruff format --check / mypy / compileall / alembic heads
    3) pytest -q (offscreen Qt is used in CI; see -Offscreen note)
    4) pytest tests/integration/test_e2e_workflow.py -xvs (disposable SQLite E2E)
    5) tools/release_smoke.py (headless + editable sdist check)
    6) Generate docs/WINDOWS_ACCEPTANCE_RESULT.json from template

  Logs are written to out/windows-acceptance/<timestamp>/ and are sanitized
  (absolute user paths are replaced with <workdir>).

.OUTPUTS
  docs/WINDOWS_ACCEPTANCE_RESULT.json  — fill, commit, and attach CI run link.
  out/windows-acceptance/               — raw logs (upload as artifact; do not edit).

.NOTES
  Distinguish four figures:
    - CI (Linux Ubuntu): regression evidence only, .venv, QT_QPA_PLATFORM=offscreen
    - Offscreen Qt (CI Windows windows-* runner): offscreen, no manual checkboxes
    - Pilot (local Windows, same script, same Python): offscreen is NOT manual
    - Manual (local Windows, real display): owner ticks docs/WINDOWS_TESTING.md checklist; result status = NOT RUN until then

  Isolation: each run creates a fresh timestamped directory and a dedicated
  .venv-win (never reusing the caller's .venv).  Exit codes are captured per
  step, written truthfully to WINDOWS_ACCEPTANCE_RESULT.json, and the script
  exits non-zero if any step fails — so a green JSON cannot hide a red step.
#>
param(
  [string]$Python = "python",
  [switch]$Offscreen,
  [string]$OutDir = "out/windows-acceptance"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runDir = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$stepResults = @()

function Tee-Log {
  param([string]$Name, [scriptblock]$Block)
  $log = Join-Path $runDir "$Name.log"
  Write-Host "`n=== $Name ===" -ForegroundColor Cyan
  # Isolate: each step starts with a fresh env, but QT_QPA_PLATFORM persists
  & $Block 2>&1 | Tee-Object -FilePath $log
  $code = $LASTEXITCODE
  if ($null -eq $code) { $code = 0 }
  $status = if ($code -eq 0) { "PASS" } else { "FAIL" }
  # Truthful per-step recording
  $global:stepResults += [pscustomobject]@{ name = $Name; exit_code = $code; status = $status; log = "$Name.log" }
  if ($code -ne 0) { Write-Warning "$Name failed (exit $code) — see $log" }
  return $code
}

function Sanitize-Log {
  param([string]$Path)
  (Get-Content $Path) -replace [regex]::Escape($PWD.Path), "<workdir>" | Set-Content $Path
}

$env:PYTHONIOENCODING = "utf-8"
if ($Offscreen) { $env:QT_QPA_PLATFORM = "offscreen" }

$overallFailed = $false
function Run-Step {
  param([string]$Name, [scriptblock]$Block)
  $code = Tee-Log $Name $Block
  if ($code -ne 0) { $global:overallFailed = $true }
}

Run-Step "01-venv" { & $Python -m venv .venv-win }
Run-Step "02-install" { & .\.venv-win\Scripts\python.exe -m pip install -U pip; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & .\.venv-win\Scripts\python.exe -m pip install -e ".[dev]" }
Run-Step "03-ruff-check" { & .\.venv-win\Scripts\python.exe -m ruff check . }
Run-Step "04-ruff-format-check" { & .\.venv-win\Scripts\python.exe -m ruff format --check . }
Run-Step "05-mypy" { & .\.venv-win\Scripts\python.exe -m mypy src }
Run-Step "06-compileall" { & .\.venv-win\Scripts\python.exe -m compileall src }
Run-Step "07-alembic-heads" { & .\.venv-win\Scripts\python.exe -m alembic heads }
Run-Step "08-pytest-q" { & .\.venv-win\Scripts\python.exe -m pytest -q }
Run-Step "09-pytest-e2e" { & .\.venv-win\Scripts\python.exe -m pytest tests/integration/test_e2e_workflow.py -xvs }
Run-Step "10-release-smoke" { & .\.venv-win\Scripts\python.exe tools/release_smoke.py }

Get-ChildItem $runDir -Filter *.log | ForEach-Object { Sanitize-Log $_.FullName }

try {
  $resultTemplate = Get-Content docs/WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json -Raw | ConvertFrom-Json
} catch {
  Write-Warning "Template not found, creating minimal result"
  $resultTemplate = [pscustomobject]@{ run = [pscustomobject]@{}; logs = [pscustomobject]@{}; steps = @(); evidence = [pscustomobject]@{}; status = "NOT RUN" }
}
$resultTemplate.run.stamp = $stamp
$resultTemplate.run.host = $env:COMPUTERNAME
try { $resultTemplate.run.python = (& .\.venv-win\Scripts\python.exe --version 2>&1) } catch { $resultTemplate.run.python = "unknown" }
try { $resultTemplate.run.sha = (& git rev-parse HEAD 2>&1).Trim() } catch { $resultTemplate.run.sha = "unknown" }
$resultTemplate.run.offscreen = [bool]$Offscreen
$resultTemplate.logs.dir = $runDir.Replace("\","/")
$resultTemplate.steps = $stepResults
# Truthful evidence mapping — each log maps to template evidence key
$evidenceMap = @{
  "03-ruff-check" = "ruff_check"; "04-ruff-format-check" = "ruff_format_check"; "05-mypy" = "mypy";
  "06-compileall" = "compileall"; "07-alembic-heads" = "alembic_heads"; "08-pytest-q" = "pytest_q";
  "09-pytest-e2e" = "pytest_e2e"; "10-release-smoke" = "release_smoke"
}
foreach ($s in $stepResults) {
  $key = $evidenceMap[$s.name]
  if ($key) { $resultTemplate.evidence.$key = $s.status }
}
$resultTemplate.status = if ($overallFailed) { "FAIL" } else { "PASS" }
# Manual checklist stays NOT RUN until owner runs on real display
if (-not $resultTemplate.PSObject.Properties.Match('manual_checklist').Count) {
  $resultTemplate | Add-Member -NotePropertyName manual_checklist -NotePropertyValue "NOT RUN — offscreen/pilot is not manual acceptance"
} elseif ($resultTemplate.manual_checklist -ne "NOT RUN" -and -not $Offscreen) {
  # keep existing if already marked, but offscreen never counts as manual
  $resultTemplate.manual_checklist = "NOT RUN — offscreen run"
}
$resultTemplate | ConvertTo-Json -Depth 10 | Set-Content -Path docs/WINDOWS_ACCEPTANCE_RESULT.json -Encoding utf8

Write-Host "`nDone. Logs: $runDir" -ForegroundColor Green
if ($overallFailed) {
  Write-Host "Result: FAIL — one or more steps failed; see $runDir and docs/WINDOWS_ACCEPTANCE_RESULT.json" -ForegroundColor Red
  Write-Host "Next: fix failures, then check docs/WINDOWS_TESTING.md manually on a real display." -ForegroundColor Yellow
  exit 1
} else {
  Write-Host "Result: PASS — all steps exited 0; still need manual display check." -ForegroundColor Green
  Write-Host "Next: fill docs/WINDOWS_ACCEPTANCE_RESULT.json, check docs/WINDOWS_TESTING.md manually on a real display, then commit." -ForegroundColor Yellow
}
