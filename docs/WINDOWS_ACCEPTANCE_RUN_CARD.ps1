#Requires -Version 5.1
<#
.SYNOPSIS
  Operator run-card for Drilling Intelligence — Windows acceptance (V9.0).
  Single PowerShell entry point. No prerequisites beyond PowerShell 5+ and Python 3.11+.

.DESCRIPTION
  Rebuilds venv, runs the four evidence steps and writes a machine-readable
  result file that matches docs/WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json.

  Steps:
    1) python -m venv .venv-win && .venv-win pip install -e .[dev]
    2) python -m pytest -q                      (offscreen Qt is used in CI; see -Offscreen note)
    3) python -m pytest tests/integration -q --run-manual-acceptance  (disposable SQLite E2E, see test_e2e_workflow)
    4) python tools/release_smoke.py            (headless + editable sdist check)
    5) Generate docs/WINDOWS_ACCEPTANCE_RESULT.json from template

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
function Tee-Log {
  param([string]$Name, [scriptblock]$Block)
  $log = Join-Path $runDir "$Name.log"
  Write-Host "`n=== $Name ===" -ForegroundColor Cyan
  & $Block 2>&1 | Tee-Object -FilePath $log
  if ($LASTEXITCODE -ne 0) { Write-Warning "$Name failed (exit $LASTEXITCODE) — see $log" }
}
function Sanitize-Log {
  param([string]$Path)
  (Get-Content $Path) -replace [regex]::Escape($PWD.Path), "<workdir>" | Set-Content $Path
}

$env:PYTHONIOENCODING = "utf-8"
if ($Offscreen) { $env:QT_QPA_PLATFORM = "offscreen" }

Tee-Log "01-venv" { & $Python -m venv .venv-win }
Tee-Log "02-install" { & .\.venv-win\Scripts\python.exe -m pip install -U pip; & .\.venv-win\Scripts\python.exe -m pip install -e ".[dev]" }
Tee-Log "03-ruff-check" { & .\.venv-win\Scripts\python.exe -m ruff check . }
Tee-Log "04-ruff-format-check" { & .\.venv-win\Scripts\python.exe -m ruff format --check . }
Tee-Log "05-mypy" { & .\.venv-win\Scripts\python.exe -m mypy src }
Tee-Log "06-compileall" { & .\.venv-win\Scripts\python.exe -m compileall src }
Tee-Log "07-pytest-q" { & .\.venv-win\Scripts\python.exe -m pytest -q }
Tee-Log "08-pytest-e2e" { & .\.venv-win\Scripts\python.exe -m pytest tests/integration/test_e2e_workflow.py -xvs }
Tee-Log "09-release-smoke" { & .\.venv-win\Scripts\python.exe tools/release_smoke.py }

Get-ChildItem $runDir -Filter *.log | ForEach-Object { Sanitize-Log $_.FullName }

$resultTemplate = Get-Content docs/WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json -Raw | ConvertFrom-Json
$resultTemplate.run.stamp = $stamp
$resultTemplate.run.host = $env:COMPUTERNAME
$resultTemplate.run.python = (& .\.venv-win\Scripts\python.exe --version)
$resultTemplate.run.offscreen = [bool]$Offscreen
$resultTemplate.logs.dir = $runDir.Replace("\","/")
$resultTemplate | ConvertTo-Json -Depth 8 | Set-Content -Path docs/WINDOWS_ACCEPTANCE_RESULT.json -Encoding utf8

Write-Host "`nDone. Logs: $runDir" -ForegroundColor Green
Write-Host "Next: fill docs/WINDOWS_ACCEPTANCE_RESULT.json, check docs/WINDOWS_TESTING.md manually on a real display, then commit." -ForegroundColor Yellow
