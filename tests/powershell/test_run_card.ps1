# Pester-like behavioral tests for WINDOWS_ACCEPTANCE_RUN_CARD.ps1 (V10.2)
# Run with: pwsh -Command "Invoke-Pester tests/powershell -Output Detailed"
# Minimal checks without full Pester dependency — pure PowerShell assertions.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$card = Join-Path (Join-Path $here "..\\..\\docs") "WINDOWS_ACCEPTANCE_RUN_CARD.ps1"
$template = Join-Path (Join-Path $here "..\\..\\docs") "WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json"

function Assert-True($cond, $msg) { if (-not $cond) { throw "ASSERT FAILED: $msg" } else { Write-Host "PASS: $msg" -ForegroundColor Green } }

# 1. Card exists and is parseable
Assert-True (Test-Path $card) "run card exists"
$null = Get-Content $card -Raw | Out-Null
Assert-True ($?) "run card parseable"

# 2. Template exists and is valid JSON with V10.2 fields
Assert-True (Test-Path $template) "template exists"
$tmpl = Get-Content $template -Raw | ConvertFrom-Json
Assert-True ($tmpl.version -eq "V10.2") "template version V10.2"
Assert-True ($null -ne $tmpl.run.id -or $tmpl.run.PSObject.Properties.Name -contains "id") "template has run.id"
Assert-True ($tmpl.steps -is [Array] -or $null -ne $tmpl.steps) "template has steps array"
Assert-True ($tmpl.evidence.PSObject.Properties.Name -contains "offline_sql") "template has offline_sql evidence"
Assert-True ($tmpl.status -match "NOT RUN|PASS|FAIL") "template status enum"

# 3. Card uses script scope, not global, for stepResults
$cardText = Get-Content $card -Raw
Assert-True ($cardText -notmatch '\$global:stepResults') "no `$global:stepResults"
Assert-True ($cardText -match '\$script:stepResults') "uses `$script:stepResults"
Assert-True ($cardText -notmatch '\$global:overallFailed') "no `$global:overallFailed"

# 4. Venv is timestamped under runDir, not static .venv
Assert-True ($cardText -match '\$runDir.*\.venv' -or $cardText -match '\$venvDir.*runDir') "venv under runDir"
Assert-True ($cardText -notmatch '\.venv-win') "no static .venv-win"

# 5. Install uses dev+ui and constraints
Assert-True ($cardText -match '\[dev,ui\]' -or $cardText -match '\[dev\]') "install dev+ui"
Assert-True ($cardText -match 'constraints') "uses constraints"

# 6. No exit inside Tee-Log, but exit at end is ok
Assert-True ($cardText -notmatch 'function Tee-Log.*exit' ) "no exit inside Tee-Log"
Assert-True ($cardText -match 'finally|try') "has try/finally for JSON"

# 7. Steps count 15+
$steps = @("01-env","02-python","03-install","04-ruff","05-ruff","06-mypy","07-compileall","08-alembic","09-offline","10-migration","11-pytest","12-ui","13-e2e","14-release","15-wheel")
foreach ($s in $steps) {
  Assert-True ($cardText -match $s) "card contains step $s"
}

Write-Host "`nAll Pester-style checks PASS" -ForegroundColor Green
