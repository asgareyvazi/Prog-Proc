# PowerShell 5.1/7 AST and behavioral acceptance tests for the V10.3 run card.
# Run: powershell -NoProfile -File tests/powershell/test_run_card.ps1
#      pwsh       -NoProfile -File tests/powershell/test_run_card.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here "..\..")).Path
$card = Join-Path $repo "docs\WINDOWS_ACCEPTANCE_RUN_CARD.ps1"
$template = Join-Path $repo "docs\WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json"

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERT FAILED: $Message" }
    Write-Host "PASS: $Message" -ForegroundColor Green
}

Assert-True (Test-Path -LiteralPath $card) "V10.3 run card exists"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($card, [ref]$tokens, [ref]$parseErrors)
$parseDetails = @($parseErrors | ForEach-Object {
    "line $($_.Extent.StartLineNumber), column $($_.Extent.StartColumnNumber): $($_.Message) [$($_.Extent.Text)]"
}) -join "; "
$parseMessage = "PowerShell parser accepts the run card"
if ($parseDetails) { $parseMessage += ": $parseDetails" }
Assert-True ($parseErrors.Count -eq 0) $parseMessage

# An exit statement in an Invoke-Step scriptblock can bypass result serialization.
$exitNodes = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.ExitStatementAst] }, $true))
Assert-True ($exitNodes.Count -eq 1) "only the outer script has an exit statement"
$node = $exitNodes[0]
while ($null -ne $node.Parent -and $node.Parent -isnot [System.Management.Automation.Language.ScriptBlockAst]) {
    Assert-True ($node.Parent -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst]) "exit is not inside a step/action scriptblock"
    Assert-True ($node.Parent -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) "exit is not inside a helper function"
    $node = $node.Parent
}

# Verify the actual Step 10 AST: SQL is captured by the native adapter, not passed as a shell token.
$step10 = $ast.FindAll({
    param($item)
    $item -is [System.Management.Automation.Language.CommandAst] -and
    $item.GetCommandName() -eq "Invoke-Step" -and
    $item.Extent.Text -match '10-offline-sql'
}, $true) | Select-Object -First 1
Assert-True ($null -ne $step10) "offline SQL step is present"
Assert-True ($step10.Extent.Text -match 'StdoutArtifact\s+\$sqlArtifact') "offline SQL is written from captured stdout"
Assert-True ($step10.Extent.Text -notmatch 'Arguments?[^\r\n]*>') "offline SQL does not pass > as a Python argument"

# The result contract is machine-readable and requires every field for every step.
$templateObject = Get-Content -LiteralPath $template -Raw | ConvertFrom-Json
Assert-True ($templateObject.version -eq "V10.3") "result template version is V10.3"
$requiredStepFields = @("id", "name", "ran", "exit_code", "started_at", "ended_at", "status", "log", "message")
foreach ($field in $requiredStepFields) {
    Assert-True ($templateObject.steps[0].PSObject.Properties.Name -contains $field) "result step contract includes $field"
}
Assert-True ($templateObject.windows_testing_checklist.display -like "NOT RUN*") "physical display checklist remains NOT RUN"
Assert-True ($templateObject.figures.manual_windows_display -like "NOT RUN*") "offscreen cannot imply physical display acceptance"

# Execute the actual process adapter without executing the full product run card.
# The function bodies are extracted from the parsed source AST, then invoked with a temp artifact dir.
foreach ($functionName in @("Get-UtcNow", "ConvertTo-WindowsArgument", "Write-Utf8File", "Invoke-NativeProcess", "Invoke-Step")) {
    $definition = $ast.FindAll({
        param($item)
        $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $functionName
    }, $true) | Select-Object -First 1
    Assert-True ($null -ne $definition) "helper $functionName is defined"
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:RepoRoot = $repo
$script:RunDir = Join-Path (Join-Path $repo "out\windows-acceptance") ("ps-adapter-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $script:RunDir -Force | Out-Null
$script:RawLogs = New-Object 'System.Collections.Generic.List[string]'
$script:StepResults = New-Object 'System.Collections.Generic.List[object]'
$script:StopSteps = $false
$script:ProcessTimeoutSeconds = 15
$enginePath = (Get-Process -Id $PID).Path
$probe = Invoke-NativeProcess -FilePath $enginePath -ArgumentList @(
    "-NoProfile", "-NonInteractive", "-Command",
    "[Console]::Out.WriteLine('stdout-probe'); [Console]::Error.WriteLine('stderr-probe'); exit 7"
) -StepId "native-nonzero" -TimeoutSeconds 15
Assert-True ($probe.Status -eq "FAIL" -and $probe.ExitCode -eq 7) "native nonzero exit is returned truthfully"
Assert-True ($probe.Stdout -match "stdout-probe") "native stdout is captured"
Assert-True ($probe.Stderr -match "stderr-probe") "native stderr is captured separately"
Assert-True (Test-Path (Join-Path $script:RunDir "native-nonzero.stdout.raw.log")) "raw stdout log is persisted"
Assert-True (Test-Path (Join-Path $script:RunDir "native-nonzero.stderr.raw.log")) "raw stderr log is persisted"

$script:ActiveStepId = "step-contract"
$script:StopSteps = $false
$step = Invoke-Step -Id "behavior-pass" -Name "structured return" -Action {
    [pscustomobject]@{ Status = "PASS"; ExitCode = 0; Ran = $true; Log = "probe.log"; Message = "ok" }
}
Assert-True ($step.status -eq "PASS" -and $step.ran -and $step.exit_code -eq 0) "successful action produces a truthful step record"
$script:StopSteps = $false
$failedInstall = Invoke-Step -Id "behavior-install-failure" -Name "failed UI install" -Action {
    [pscustomobject]@{ Status = "FAIL"; ExitCode = 1; Ran = $true; Log = "install.log"; Message = "UI package unavailable" }
}
Assert-True ($failedInstall.status -eq "FAIL" -and $failedInstall.exit_code -eq 1) "failed install remains FAIL"
$notRun = Invoke-Step -Id "behavior-not-run" -Name "dependent pytest" -Action {
    throw "this action must not execute"
}
Assert-True ($notRun.status -eq "NOT RUN" -and -not $notRun.ran -and $null -eq $notRun.exit_code) "dependent test is NOT RUN after install failure"
$script:StopSteps = $false
$failedPytest = Invoke-Step -Id "behavior-pytest-failure" -Name "failed pytest" -Action {
    [pscustomobject]@{ Status = "FAIL"; ExitCode = 1; Ran = $true; Log = "pytest.log"; Message = "zero tests or failed suite" }
}
Assert-True ($failedPytest.status -eq "FAIL" -and $failedPytest.exit_code -eq 1) "pytest failure cannot be turned into PASS"
$script:StopSteps = $false
$cancelled = Invoke-Step -Id "behavior-cancelled" -Name "cancelled native process" -Action {
    [pscustomobject]@{ Status = "CANCELLED"; ExitCode = $null; Ran = $true; Log = "cancelled.log"; Message = "deadline reached" }
}
Assert-True ($cancelled.status -eq "CANCELLED" -and $null -eq $cancelled.exit_code) "cancelled process is not PASS"

# Run the actual script twice with a missing Python executable. Both executions must produce
# result JSON; each gets a unique run ID and isolated .venv path, while later steps stay NOT RUN.
$harnessOut = Join-Path $repo "out\windows-acceptance\behavioral-runs"
$before = @()
if (Test-Path $harnessOut) { $before = @(Get-ChildItem -LiteralPath $harnessOut -Directory | Select-Object -ExpandProperty FullName) }
$firstCode = 0
& $enginePath -NoProfile -NonInteractive -File $card -Python "__progproc_missing_python_for_test__" -OutDir "out/windows-acceptance/behavioral-runs"
$firstCode = $LASTEXITCODE
Assert-True ($firstCode -ne 0) "missing Python produces a nonzero run-card result"
$allDirs = @(Get-ChildItem -LiteralPath $harnessOut -Directory)
$firstDir = $allDirs | Where-Object { $before -notcontains $_.FullName } | Select-Object -First 1
Assert-True ($null -ne $firstDir) "first run creates an isolated evidence directory"
$firstResult = Get-Content -LiteralPath (Join-Path $firstDir.FullName "result.json") -Raw | ConvertFrom-Json
Assert-True ($firstResult.status -eq "FAIL") "missing Python cannot be reported as PASS"
Assert-True ($firstResult.steps[0].status -eq "FAIL" -and -not $firstResult.steps[0].ran) "missing interpreter has truthful failed step"
Assert-True (@($firstResult.steps | Where-Object { $_.status -eq "NOT RUN" }).Count -gt 0) "dependent checks are NOT RUN, not fabricated passes"
Assert-True ($firstResult.manual_checklist -like "NOT RUN*") "manual acceptance remains NOT RUN"

$firstId = $firstResult.run.id
$beforeSecond = @(Get-ChildItem -LiteralPath $harnessOut -Directory | Select-Object -ExpandProperty FullName)
$secondCode = 0
& $enginePath -NoProfile -NonInteractive -File $card -Python "__progproc_missing_python_for_test__" -OutDir "out/windows-acceptance/behavioral-runs"
$secondCode = $LASTEXITCODE
Assert-True ($secondCode -ne 0) "second missing-Python run also fails honestly"
$allDirs = @(Get-ChildItem -LiteralPath $harnessOut -Directory)
$secondDir = $allDirs | Where-Object { $beforeSecond -notcontains $_.FullName } | Select-Object -First 1
Assert-True ($null -ne $secondDir) "second run receives a separate isolated evidence directory"
$secondResult = Get-Content -LiteralPath (Join-Path $secondDir.FullName "result.json") -Raw | ConvertFrom-Json
Assert-True ($secondResult.run.id -ne $firstId) "two consecutive run IDs differ"
Assert-True ($secondResult.run.venv_path -ne $firstResult.run.venv_path) "two consecutive virtual environments differ"

# Invalid templates fail the run but must not prevent final result JSON from being written.
$invalidTemplate = Join-Path $harnessOut ("invalid-template-" + [Guid]::NewGuid().ToString("N") + ".json")
Set-Content -LiteralPath $invalidTemplate -Value "{ invalid json" -Encoding UTF8
$beforeInvalid = @(Get-ChildItem -LiteralPath $harnessOut -Directory | Select-Object -ExpandProperty FullName)
& $enginePath -NoProfile -NonInteractive -File $card -Python "__progproc_missing_python_for_test__" -OutDir "out/windows-acceptance/behavioral-runs" -ResultTemplate $invalidTemplate
Assert-True ($LASTEXITCODE -ne 0) "invalid result template makes the run fail closed"
$afterInvalid = @(Get-ChildItem -LiteralPath $harnessOut -Directory)
$invalidDir = $afterInvalid | Where-Object { $beforeInvalid -notcontains $_.FullName } | Select-Object -First 1
Assert-True ($null -ne $invalidDir) "invalid template run still creates evidence directory"
$invalidResult = Get-Content -LiteralPath (Join-Path $invalidDir.FullName "result.json") -Raw | ConvertFrom-Json
Assert-True ($invalidResult.status -eq "FAIL") "invalid template cannot produce a green result"
Assert-True (@($invalidResult.blocked_reasons | Where-Object { $_ -like "result template invalid:*" }).Count -eq 1) "invalid template reason is recorded"

# If the destination itself cannot be created, the outer runner still exits nonzero and never
# claims a result. A unique file is used as a deliberate non-directory parent.
$notDirectoryName = "not-a-directory-" + [Guid]::NewGuid().ToString("N")
$notDirectory = Join-Path (Join-Path $repo "out/windows-acceptance") $notDirectoryName
Set-Content -LiteralPath $notDirectory -Value "sentinel" -Encoding ASCII
& $enginePath -NoProfile -NonInteractive -File $card -Python "__progproc_missing_python_for_test__" -OutDir ("out/windows-acceptance/" + $notDirectoryName)
Assert-True ($LASTEXITCODE -ne 0) "uncreatable result directory cannot be reported as success"

# The last native child intentionally exits nonzero; all assertions above verified that result.
# Clear the automatic native exit status so this successful test script itself exits zero.
$global:LASTEXITCODE = 0
Write-Host "All V10.3 run-card AST and behavioral tests PASS on PowerShell $($PSVersionTable.PSVersion)" -ForegroundColor Green
