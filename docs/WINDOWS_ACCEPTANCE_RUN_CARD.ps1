#Requires -Version 5.1
<#
.SYNOPSIS
  Isolated Windows acceptance run card for Prog-Proc V10.3.
.DESCRIPTION
  Every invocation receives out/windows-acceptance/<run-id>/.venv and its own raw,
  sanitized, SQL, JUnit, package, and result artifacts.  The native-process adapter
  returns a structured terminal result and uses a finite timeout.  A failed prerequisite
  leaves dependent steps NOT RUN.  No step block calls exit; one outer exit follows the
  final JSON write.  UI qualification fails if PySide6 or its Qt platform cannot load.

  Invoke from the repository root (or any directory):
    powershell -NoProfile -File docs/WINDOWS_ACCEPTANCE_RUN_CARD.ps1 -Python C:\Python311\python.exe
    pwsh       -NoProfile -File docs/WINDOWS_ACCEPTANCE_RUN_CARD.ps1 -Python C:\Python314\python.exe

  This run card's offscreen result is not real-display acceptance. The owner checklist stays
  NOT RUN until an owner performs it on a Windows workstation with a real display.
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Offscreen,
    [string]$OutDir = "out/windows-acceptance",
    [string]$ResultTemplate = "",
    [ValidateRange(30, 7200)][int]$ProcessTimeoutSeconds = 3600
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:StartedAt = [DateTime]::UtcNow.ToString("o")
$script:RunId = [Guid]::NewGuid().ToString("N")
$script:RunDir = Join-Path (Join-Path $script:RepoRoot $OutDir) $script:RunId
$script:VenvDir = Join-Path $script:RunDir ".venv"
$script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"
$script:VenvScripts = Join-Path $script:VenvDir "Scripts"
$script:StepResults = New-Object 'System.Collections.Generic.List[object]'
$script:StopSteps = $false
$script:PythonExe = $null
$script:PythonVersion = "NOT RUN"
$script:QtProbeText = "NOT RUN"
$script:RawLogs = New-Object 'System.Collections.Generic.List[string]'
$script:ResultWriteFailed = $false
$script:LocationPushed = $false

function Get-UtcNow {
    return [DateTime]::UtcNow.ToString("o")
}

function ConvertTo-WindowsArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2 + 1))) }
            else { [void]$builder.Append('\') }
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * $slashes))
            $slashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Write-Utf8File {
    param([string]$Path, [AllowEmptyString()][string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Invoke-NativeProcess {
    <# A single native boundary: no shell interpolation, separate streams, finite wait. #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$StepId,
        [hashtable]$Environment = @{},
        [string]$StdoutArtifact = "",
        [ValidateRange(1, 7200)][int]$TimeoutSeconds = $ProcessTimeoutSeconds
    )
    $stdoutPath = Join-Path $script:RunDir "$StepId.stdout.raw.log"
    $stderrPath = Join-Path $script:RunDir "$StepId.stderr.raw.log"
    $script:RawLogs.Add($stdoutPath)
    $script:RawLogs.Add($stderrPath)
    $started = Get-UtcNow
    $process = $null
    $exitCode = $null
    $status = "FAIL"
    $message = ""
    $stdout = ""
    $stderr = ""
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $FilePath
        $quoted = @()
        foreach ($argument in $ArgumentList) { $quoted += (ConvertTo-WindowsArgument ([string]$argument)) }
        $info.Arguments = [string]::Join(" ", $quoted)
        $info.WorkingDirectory = $script:RepoRoot
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        foreach ($entry in $Environment.GetEnumerator()) {
            $info.EnvironmentVariables[[string]$entry.Key] = [string]$entry.Value
        }
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $info
        if (-not $process.Start()) { throw "native process did not start" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $finished) {
            # The process gets a short graceful close opportunity before bounded cleanup.
            try { [void]$process.CloseMainWindow() } catch { }
            $finished = $process.WaitForExit(5000)
            if (-not $finished) {
                $process.Kill()
                $finished = $process.WaitForExit(5000)
            }
            if ($finished) {
                $stdout = $stdoutTask.GetAwaiter().GetResult()
                $stderr = $stderrTask.GetAwaiter().GetResult()
            } else {
                $stdout = ""
                $stderr = "process streams closed after bounded cancellation attempt"
            }
            $status = "CANCELLED"
            $message = "native process exceeded ${TimeoutSeconds}s bounded deadline"
        } else {
            $process.WaitForExit()
            $stdout = $stdoutTask.GetAwaiter().GetResult()
            $stderr = $stderrTask.GetAwaiter().GetResult()
            $exitCode = [int]$process.ExitCode
            $status = if ($exitCode -eq 0) { "PASS" } else { "FAIL" }
            $message = if ($exitCode -eq 0) { "native process completed" } else { "native exit code $exitCode" }
        }
    } catch {
        $message = "native process start/capture failed: $($_.Exception.Message)"
        $stderr = $message
        $status = "FAIL"
        if ($null -eq $exitCode) { $exitCode = 1 }
    } finally {
        if ($null -ne $process) { $process.Dispose() }
        Write-Utf8File -Path $stdoutPath -Text $stdout
        Write-Utf8File -Path $stderrPath -Text $stderr
        if ($StdoutArtifact) { Write-Utf8File -Path $StdoutArtifact -Text $stdout }
    }
    return [pscustomobject]@{
        Status = $status
        ExitCode = $exitCode
        StartedAt = $started
        EndedAt = Get-UtcNow
        Stdout = $stdout
        Stderr = $stderr
        Log = "$StepId.stdout.raw.log; $StepId.stderr.raw.log"
        Message = $message
        Ran = ($status -ne "NOT RUN")
    }
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$StdoutArtifact = "",
        [ValidateRange(1, 7200)][int]$TimeoutSeconds = $ProcessTimeoutSeconds,
        [hashtable]$Environment = @{}
    )
    if (-not $script:VenvPython -or -not (Test-Path $script:VenvPython)) {
        return [pscustomobject]@{
            Status = "BLOCKED"; ExitCode = $null; StartedAt = Get-UtcNow; EndedAt = Get-UtcNow
            Stdout = ""; Stderr = "isolated Python interpreter is not available"
            Log = ""; Message = "isolated Python interpreter is not available"; Ran = $false
        }
    }
    return Invoke-NativeProcess -FilePath $script:VenvPython -ArgumentList $ArgumentList `
        -StepId $script:ActiveStepId -Environment $Environment `
        -StdoutArtifact $StdoutArtifact -TimeoutSeconds $TimeoutSeconds
}

function Invoke-PytestAndVerify {
    param([string[]]$Selection, [string]$StepId, [int]$TimeoutSeconds = $ProcessTimeoutSeconds)
    $junit = Join-Path $script:RunDir "$StepId.junit.xml"
    $stats = Join-Path $script:RunDir "$StepId.stats.json"
    $exitFile = Join-Path $script:RunDir "$StepId.exit.txt"
    $arguments = @("-m", "pytest", "-q", "--junitxml=$junit") + $Selection
    $testResult = Invoke-Python -ArgumentList $arguments -TimeoutSeconds $TimeoutSeconds `
        -Environment @{ PYTEST_TOTALS_PATH = $stats }
    if ($null -ne $testResult.ExitCode) {
        Set-Content -Path $exitFile -Value ([string]$testResult.ExitCode) -Encoding ASCII
    }
    if ($testResult.Status -eq "CANCELLED") { return $testResult }
    if ($testResult.Status -eq "BLOCKED") { return $testResult }
    $totals = Invoke-Python -ArgumentList @(
        "tools/pytest_totals.py", "--stats", $stats, "--junit", $junit, "--exit-file", $exitFile
    ) -TimeoutSeconds 120
    if ($testResult.Status -ne "PASS") { return $testResult }
    if ($totals.Status -ne "PASS") { return $totals }
    return $totals
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $started = Get-UtcNow
    $script:ActiveStepId = $Id
    if ($script:StopSteps) {
        $record = [pscustomobject]@{
            id = $Id; name = $Name; ran = $false; exit_code = $null
            started_at = $started; ended_at = Get-UtcNow; status = "NOT RUN"
            log = ""; message = "not run because a prerequisite step failed"
        }
        $script:StepResults.Add($record)
        return $record
    }
    Write-Host "`n=== $Id - $Name ===" -ForegroundColor Cyan
    $outcome = $null
    try {
        $outcome = & $Action
        if ($outcome -is [array]) { $outcome = $outcome | Select-Object -Last 1 }
        if ($null -eq $outcome -or $null -eq $outcome.PSObject.Properties["Status"]) {
            throw "step did not return a structured native-process outcome"
        }
        if ($Id -eq "01-python" -and $outcome.Stdout) { $script:PythonVersion = $outcome.Stdout.Trim() }
        if ($Id -eq "04-qt-probe" -and $outcome.Stdout) { $script:QtProbeText = $outcome.Stdout.Trim() }
    } catch {
        $outcome = [pscustomobject]@{
            Status = "FAIL"; ExitCode = 1; StartedAt = $started; EndedAt = Get-UtcNow
            Log = "$Id.error.log"; Message = $_.Exception.Message; Ran = $true
        }
        Write-Utf8File -Path (Join-Path $script:RunDir "$Id.error.log") -Text $outcome.Message
    }
    $record = [pscustomobject]@{
        id = $Id; name = $Name; ran = [bool]$outcome.Ran; exit_code = $outcome.ExitCode
        started_at = $started; ended_at = Get-UtcNow; status = [string]$outcome.Status
        log = [string]$outcome.Log; message = [string]$outcome.Message
    }
    $script:StepResults.Add($record)
    if ($record.status -notin @("PASS", "SKIPPED")) { $script:StopSteps = $true }
    if ($outcome.Stdout) { Write-Host $outcome.Stdout }
    if ($outcome.Stderr) { Write-Host $outcome.Stderr }
    Write-Host "$Id $($record.status) (exit=$($record.exit_code))"
    return $record
}

function Invoke-PackageSmoke {
    param([string]$PackagePath, [string]$Label)
    $cleanVenv = Join-Path $script:RunDir "$Label-install-venv"
    $cleanPython = Join-Path $cleanVenv "Scripts\python.exe"
    $create = Invoke-NativeProcess -FilePath $script:PythonExe -ArgumentList @("-m", "venv", $cleanVenv) -StepId "$($script:ActiveStepId)-$Label-venv"
    if ($create.Status -ne "PASS") { return $create }
    $install = Invoke-NativeProcess -FilePath $cleanPython -ArgumentList @("-m", "pip", "install", $PackagePath) -StepId "$($script:ActiveStepId)-$Label-install"
    if ($install.Status -ne "PASS") { return $install }
    $check = Invoke-NativeProcess -FilePath $cleanPython -ArgumentList @(
        "-c",
        'import importlib; revision = importlib.import_module(''drilling_intelligence.database.migrations'').METADATA_REVISION; assert revision == ''0024''; print(revision)'
    ) -StepId "$($script:ActiveStepId)-$Label-import"
    if ($check.Status -ne "PASS") { return $check }
    $cli = Join-Path $cleanVenv "Scripts\drillintel.exe"
    $help = Invoke-NativeProcess -FilePath $cli -ArgumentList @("--help") -StepId "$($script:ActiveStepId)-$Label-cli"
    if ($help.Status -ne "PASS") { return $help }
    $workspace = Join-Path $script:RunDir "$Label-workspace"
    $createWorkspace = Invoke-NativeProcess -FilePath $cli -ArgumentList @("workspace", "create", $workspace) -StepId "$($script:ActiveStepId)-$Label-bootstrap"
    if ($createWorkspace.Status -ne "PASS") { return $createWorkspace }
    $doctor = Invoke-NativeProcess -FilePath $cli -ArgumentList @("doctor", "--workspace", $workspace, "--json") -StepId "$($script:ActiveStepId)-$Label-doctor"
    if ($doctor.Status -ne "PASS") { return $doctor }
    try {
        $payload = $doctor.Stdout | ConvertFrom-Json
        if ($payload.schema.current -ne "0024" -or -not $payload.schema.up_to_date) {
            throw "clean $Label package bootstrap/Doctor did not report 0024 current"
        }
    } catch {
        return [pscustomobject]@{
            Status = "FAIL"; ExitCode = 1; StartedAt = Get-UtcNow; EndedAt = Get-UtcNow
            Stdout = $doctor.Stdout; Stderr = $doctor.Stderr; Log = $doctor.Log
            Message = $_.Exception.Message; Ran = $true
        }
    }
    $noUiDependency = Invoke-NativeProcess -FilePath $cleanPython -ArgumentList @(
        "-c", "import importlib.util; assert importlib.util.find_spec('PySide6') is None"
    ) -StepId "$($script:ActiveStepId)-$Label-no-ui"
    if ($noUiDependency.Status -ne "PASS") { return $noUiDependency }
    $uiLauncher = Join-Path $cleanVenv "Scripts\drillintel-ui.exe"
    $uiMissing = Invoke-NativeProcess -FilePath $uiLauncher -ArgumentList @() -StepId "$($script:ActiveStepId)-$Label-ui-missing"
    if ($uiMissing.ExitCode -ne 2 -or $uiMissing.Stderr -notmatch "could not load Qt") {
        return [pscustomobject]@{
            Status = "FAIL"; ExitCode = 1; StartedAt = $uiMissing.StartedAt; EndedAt = Get-UtcNow
            Stdout = $uiMissing.Stdout; Stderr = $uiMissing.Stderr; Log = $uiMissing.Log
            Message = "missing-UI dependency behavior was not the documented exit-2 diagnostic"; Ran = $true
        }
    }
    return [pscustomobject]@{
        Status = "PASS"; ExitCode = 0; StartedAt = $doctor.StartedAt; EndedAt = Get-UtcNow
        Stdout = $doctor.Stdout; Stderr = $uiMissing.Stderr; Log = ($doctor.Log + "; " + $uiMissing.Log)
        Message = ($Label + " import, CLI, workspace bootstrap, Doctor 0024, and missing-UI behavior passed"); Ran = $true
    }
}

function Sanitize-RunLogs {
    foreach ($raw in $script:RawLogs) {
        if (-not (Test-Path $raw)) { continue }
        $content = Get-Content -LiteralPath $raw -Raw
        $sanitized = $content -replace [regex]::Escape($script:RepoRoot), "<worktree>"
        $sanitized = $sanitized -replace [regex]::Escape($env:USERPROFILE), "<user>"
        $sanitizedPath = $raw -replace '\.raw\.log$', '.sanitized.log'
        Write-Utf8File -Path $sanitizedPath -Text $sanitized
    }
}

function New-MinimalResult {
    return [pscustomobject]@{
        version = "V10.3"
        run = [pscustomobject]@{}
        ci = [pscustomobject]@{ linux_run_id = ""; linux_sha = ""; linux_conclusion = "NOT RUN"; linux_jobs = @(); windows_run_id = ""; windows_sha = ""; windows_conclusion = "NOT RUN"; windows_jobs = @() }
        steps = @()
        evidence = [pscustomobject]@{}
        logs = [pscustomobject]@{}
        figures = [pscustomobject]@{}
        windows_testing_checklist = [pscustomobject]@{ items_ticked = @(); items_not_run = @(1..16); checked_by = ""; checked_at = ""; display = "NOT RUN - requires real display" }
        manual_checklist = "NOT RUN - offscreen is not physical display acceptance"
        status = "FAIL"
        owner_decision_required = $false
        blocked_reasons = @()
    }
}

try {
    New-Item -ItemType Directory -Path $script:RunDir -Force | Out-Null
    Push-Location $script:RepoRoot
    $script:LocationPushed = $true
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    if ($Offscreen) { $env:QT_QPA_PLATFORM = "offscreen" }

    # 01 - Resolve and execute the selected interpreter; missing Python is FAIL, never PASS.
    $command = Get-Command $Python -ErrorAction SilentlyContinue
    Invoke-Step -Id "01-python" -Name "Resolve Python and record version" -Action {
        if ($null -eq $command) {
            return [pscustomobject]@{ Status = "FAIL"; ExitCode = 127; StartedAt = Get-UtcNow; EndedAt = Get-UtcNow; Stdout = ""; Stderr = "Python not found: $Python"; Log = ""; Message = "Python not found: $Python"; Ran = $false }
        }
        $script:PythonExe = [string]$command.Source
        return Invoke-NativeProcess -FilePath $script:PythonExe -ArgumentList @("--version") -StepId $script:ActiveStepId
    } | Out-Null

    # 02 - A fresh virtual environment for every run; no project-root .venv reuse.
    Invoke-Step -Id "02-venv" -Name "Create isolated run environment" -Action {
        if (-not $script:PythonExe) { throw "Python executable was not resolved" }
        $result = Invoke-NativeProcess -FilePath $script:PythonExe -ArgumentList @("-m", "venv", $script:VenvDir) -StepId $script:ActiveStepId
        if ($result.Status -eq "PASS" -and -not (Test-Path $script:VenvPython)) {
            throw "venv command returned success but Scripts/python.exe is missing"
        }
        return $result
    } | Out-Null

    # 03 - UI extra is mandatory for a UI-qualified run; never fall back to [dev].
    Invoke-Step -Id "03-install-dev-ui" -Name "Install pinned development and UI dependencies" -Action {
        $constraints = @(
            "-c", (Join-Path $script:RepoRoot "constraints/development.txt"),
            "-c", (Join-Path $script:RepoRoot "constraints/ui.txt"),
            "-c", (Join-Path $script:RepoRoot "constraints/build.txt")
        )
        $upgradePip = Invoke-Python -ArgumentList (@("-m", "pip", "install", "--upgrade", "pip")) -TimeoutSeconds 600
        if ($upgradePip.Status -ne "PASS") { return $upgradePip }
        return Invoke-Python -ArgumentList (@("-m", "pip", "install") + $constraints + @("-e", ".[dev,ui]")) -TimeoutSeconds 1800
    } | Out-Null

    Invoke-Step -Id "04-qt-probe" -Name "Verify actual PySide6 and Qt runtime" -Action {
        return Invoke-Python -ArgumentList @(
            "-c",
            "import PySide6; from PySide6.QtCore import qVersion; from PySide6.QtWidgets import QApplication; print(PySide6.__version__, qVersion()); app=QApplication.instance() or QApplication([]); print(app.platformName())"
        ) -TimeoutSeconds 120
    } | Out-Null

    Invoke-Step -Id "05-ruff" -Name "Ruff lint" -Action { return Invoke-Python -ArgumentList @("-m", "ruff", "check", ".") } | Out-Null
    Invoke-Step -Id "06-format" -Name "Ruff format check" -Action { return Invoke-Python -ArgumentList @("-m", "ruff", "format", "--check", ".") } | Out-Null
    Invoke-Step -Id "07-mypy" -Name "Declared mypy boundary" -Action { return Invoke-Python -ArgumentList @("-m", "mypy") -TimeoutSeconds 600 } | Out-Null
    Invoke-Step -Id "08-compile" -Name "Compile source, tests, migrations and tools" -Action { return Invoke-Python -ArgumentList @("-m", "compileall", "-q", "src", "tests", "migrations", "tools") } | Out-Null
    Invoke-Step -Id "09-alembic-head" -Name "Verify one Alembic head" -Action { return Invoke-Python -ArgumentList @("-m", "alembic", "heads") } | Out-Null

    $sqlArtifact = Join-Path $script:RunDir "10-offline.sql"
    Invoke-Step -Id "10-offline-sql" -Name "Render offline SQL with stdout captured as SQL" -Action {
        $render = Invoke-Python -ArgumentList @("-m", "alembic", "upgrade", "head", "--sql") -StdoutArtifact $sqlArtifact -TimeoutSeconds 300
        if ($render.Status -ne "PASS") { return $render }
        if (-not (Test-Path $sqlArtifact) -or (Get-Item $sqlArtifact).Length -lt 100 -or (Get-Content $sqlArtifact -Raw) -notmatch "CREATE TABLE|ALTER TABLE") {
            throw "offline SQL process passed but SQL artifact is missing, empty, or malformed"
        }
        return $render
    } | Out-Null

    $migrationWorkspace = Join-Path $script:RunDir "migration-workspace"
    Invoke-Step -Id "11-migration-doctor" -Name "Bootstrap workspace and verify Doctor migration head" -Action {
        $cli = Join-Path $script:VenvScripts "drillintel.exe"
        $created = Invoke-NativeProcess -FilePath $cli -ArgumentList @("workspace", "create", $migrationWorkspace) -StepId "$($script:ActiveStepId)-create"
        if ($created.Status -ne "PASS") { return $created }
        $doctor = Invoke-NativeProcess -FilePath $cli -ArgumentList @("doctor", "--workspace", $migrationWorkspace, "--json") -StepId "$($script:ActiveStepId)-doctor"
        if ($doctor.Status -ne "PASS") { return $doctor }
        $payload = $doctor.Stdout | ConvertFrom-Json
        if ($payload.schema.current -ne "0024" -or -not $payload.schema.up_to_date) { throw "Doctor did not report migration 0024 as current" }
        return $doctor
    } | Out-Null

    Invoke-Step -Id "12-pytest-full" -Name "Full pytest with validated totals" -Action {
        return Invoke-PytestAndVerify -Selection @() -StepId $script:ActiveStepId -TimeoutSeconds $ProcessTimeoutSeconds
    } | Out-Null
    Invoke-Step -Id "13-qt-workbench" -Name "Actual Qt Workbench tests (offscreen only when requested)" -Action {
        return Invoke-PytestAndVerify -Selection @("tests/ui") -StepId $script:ActiveStepId -TimeoutSeconds 1800
    } | Out-Null
    Invoke-Step -Id "14-e2e" -Name "Disposable end-to-end operator workflow" -Action {
        return Invoke-PytestAndVerify -Selection @("tests/integration/test_e2e_workflow.py") -StepId $script:ActiveStepId -TimeoutSeconds 900
    } | Out-Null
    Invoke-Step -Id "15-release-smoke" -Name "Wheel and sdist release smoke" -Action {
        return Invoke-Python -ArgumentList @("tools/release_smoke.py") -TimeoutSeconds 1800
    } | Out-Null

    Invoke-Step -Id "16-wheel-sdist-clean-install" -Name "Build and clean-install wheel and sdist" -Action {
        $dist = Join-Path $script:RunDir "dist"
        New-Item -ItemType Directory -Path $dist -Force | Out-Null
        $built = Invoke-Python -ArgumentList @("-m", "build", "--wheel", "--sdist", "--outdir", $dist) -TimeoutSeconds 1200
        if ($built.Status -ne "PASS") { return $built }
        $wheel = Get-ChildItem -LiteralPath $dist -Filter "*.whl" | Select-Object -First 1
        $sdist = Get-ChildItem -LiteralPath $dist -Filter "*.tar.gz" | Select-Object -First 1
        if ($null -eq $wheel -or $null -eq $sdist) { throw "build completed without both wheel and sdist artifacts" }
        $wheelResult = Invoke-PackageSmoke -PackagePath $wheel.FullName -Label "wheel"
        if ($wheelResult.Status -ne "PASS") { return $wheelResult }
        return Invoke-PackageSmoke -PackagePath $sdist.FullName -Label "sdist"
    } | Out-Null
} catch {
    $script:StopSteps = $true
    $id = "00-initialization"
    $script:StepResults.Add([pscustomobject]@{
        id = $id; name = "Run initialization"; ran = $true; exit_code = 1
        started_at = $script:StartedAt; ended_at = Get-UtcNow; status = "FAIL"
        log = "$id.error.log"; message = $_.Exception.Message
    })
    if (Test-Path $script:RunDir) { Write-Utf8File -Path (Join-Path $script:RunDir "$id.error.log") -Text $_.Exception.ToString() }
} finally {
    if ($script:LocationPushed) { Pop-Location }
    Sanitize-RunLogs
    $finalStatus = "PASS"
    if (@($script:StepResults | Where-Object { $_.status -eq "FAIL" }).Count -gt 0) { $finalStatus = "FAIL" }
    elseif (@($script:StepResults | Where-Object { $_.status -eq "CANCELLED" }).Count -gt 0) { $finalStatus = "CANCELLED" }
    elseif (@($script:StepResults | Where-Object { $_.status -eq "BLOCKED" }).Count -gt 0) { $finalStatus = "BLOCKED" }
    $result = New-MinimalResult
    try {
        $templatePath = $ResultTemplate
        if (-not $templatePath) {
            $templatePath = Join-Path $script:RepoRoot "docs/WINDOWS_ACCEPTANCE_RESULT_TEMPLATE.json"
        }
        $template = Get-Content -LiteralPath $templatePath -Raw | ConvertFrom-Json
        if ($null -eq $template.steps -or $null -eq $template.run) { throw "result template is missing run/steps contract" }
    } catch {
        $finalStatus = "FAIL"
        $result.blocked_reasons += "result template invalid: $($_.Exception.Message)"
    }
    $branch = "unknown"
    $sha = "unknown"
    try { $branch = (& git -C $script:RepoRoot branch --show-current 2>$null).Trim() } catch { }
    try { $sha = (& git -C $script:RepoRoot rev-parse HEAD 2>$null).Trim() } catch { }
    $remoteState = "unknown"
    try {
        $counts = (& git -C $script:RepoRoot rev-list --left-right --count "origin/$branch...HEAD" 2>$null).Trim()
        if ($counts -match '^0\s+0$') { $remoteState = "synced" } else { $remoteState = $counts }
    } catch { }
    $result.version = "V10.3"
    $result.run = [pscustomobject]@{
        id = $script:RunId; utc_timestamp = $script:StartedAt; ended_at = Get-UtcNow
        branch = $branch; sha = $sha; remote_state = $remoteState; host = $env:COMPUTERNAME
        windows_edition = if ($env:OS) { $env:OS } else { "Windows" }
        windows_build = [System.Environment]::OSVersion.Version.ToString()
        architecture = $env:PROCESSOR_ARCHITECTURE
        powershell_version = $PSVersionTable.PSVersion.ToString()
        python_executable = if ($script:PythonExe) { $script:PythonExe } else { "NOT RUN" }
        python_version = $script:PythonVersion
        pyside6_version = $script:QtProbeText
        qt_version = $script:QtProbeText
        venv_path = $script:VenvDir.Replace('\', '/')
        offscreen = [bool]$Offscreen
    }
    $result.steps = @($script:StepResults.ToArray())
    $evidenceMap = @{
        "05-ruff" = "ruff_check"; "06-format" = "ruff_format_check"; "07-mypy" = "mypy"
        "08-compile" = "compileall"; "09-alembic-head" = "alembic_heads"; "10-offline-sql" = "offline_sql"
        "11-migration-doctor" = "migration_smoke"; "12-pytest-full" = "pytest_q"
        "13-qt-workbench" = "ui_offscreen"; "14-e2e" = "e2e_disposable"
        "15-release-smoke" = "release_smoke"; "16-wheel-sdist-clean-install" = "wheel_install"
    }
    foreach ($completedStep in $script:StepResults) {
        $evidenceKey = $evidenceMap[[string]$completedStep.id]
        if ($evidenceKey) { $result.evidence.$evidenceKey = [string]$completedStep.status }
        if ($completedStep.id -eq "16-wheel-sdist-clean-install") {
            $result.evidence.sdist_install = [string]$completedStep.status
        }
    }
    $pytestStats = Join-Path $script:RunDir "12-pytest-full.stats.json"
    if (Test-Path $pytestStats) {
        try {
            $totals = Get-Content -LiteralPath $pytestStats -Raw | ConvertFrom-Json
            $result.evidence.pytest_q_counts = [pscustomobject]@{
                collected = $totals.collected; passed = $totals.passed; failed = $totals.failed
                skipped = $totals.skipped; xfailed = $totals.xfailed; xpassed = $totals.xpassed
                exit = $totals.exit; runtime = $totals.runtime
            }
        } catch { $result.evidence.pytest_q = "FAIL" }
    }
    $result.logs = [pscustomobject]@{
        dir = $script:RunDir.Replace('\', '/')
        raw_logs = @($script:RawLogs | ForEach-Object { [System.IO.Path]::GetFileName($_) })
        sanitized = $true
        note = "Raw stdout/stderr are preserved separately; .sanitized.log copies redact worktree and user paths."
    }
    $result.status = $finalStatus
    $result.manual_checklist = "NOT RUN - offscreen/pilot is not physical Windows acceptance"
    $result.windows_testing_checklist.display = "NOT RUN - requires real display"
    try {
        $json = $result | ConvertTo-Json -Depth 20
        Write-Utf8File -Path (Join-Path $script:RunDir "result.json") -Text ($json + "`n")
        Write-Host "Result $finalStatus : $(Join-Path $script:RunDir 'result.json')"
    } catch {
        $script:ResultWriteFailed = $true
        Write-Warning "Unable to write final result JSON: $($_.Exception.Message)"
    }
}

$finalExitCode = if ($script:ResultWriteFailed -or $result.status -ne "PASS") { 1 } else { 0 }
exit $finalExitCode
