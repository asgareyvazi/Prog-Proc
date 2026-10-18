#requires -Version 5.1
<#
.SYNOPSIS
  Disposable Windows pilot for the installed CLI.

.DESCRIPTION
  Creates a unique temporary workspace, runs the documented console checks, and
  writes a redacted summary. It does not open a user-visible window and it does
  not mark the manual checklist in docs/WINDOWS_TESTING.md as run.

  Installing packages requires -AllowInstall. A provider probe requires
  -AllowProviderProbe. Neither is implied by the other switches.
#>
[CmdletBinding()]
param(
    [switch]$AllowInstall,
    [switch]$FullPytest,
    [switch]$ReleaseSmoke,
    [switch]$OffscreenUi,
    [switch]$AllowProviderProbe,
    [string]$Python = "python",
    [string]$RepoRoot = "",
    [int]$FullPytestTimeoutMinutes = 90
)

$ErrorActionPreference = "Stop"
$script:Failed = $false
$script:Steps = @()
$script:PythonExe = ""
$script:Drillintel = ""

function Write-PilotNote {
    param([string]$Message)
    Write-Output $Message
}

function Resolve-Python {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Python executable not found: $Name"
    }
    return $command.Source
}

function Redact-Text {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) {
        return ""
    }
    $file = New-TemporaryFile
    try {
        Set-Content -Path $file.FullName -Value $Text -Encoding utf8
        $env:DRILLINTEL_PILOT_REDACT_FILE = $file.FullName
        $redacted = & $script:PythonExe -c "import os; from pathlib import Path; from drilling_intelligence.core.redact import safe_detail; print(safe_detail(Path(os.environ['DRILLINTEL_PILOT_REDACT_FILE']).read_text(encoding='utf-8')))"
        if ($LASTEXITCODE -ne 0) {
            return "[redaction failed]"
        }
        return ($redacted | Out-String).Trim()
    } catch {
        return "[redaction failed]"
    } finally {
        Remove-Item Env:DRILLINTEL_PILOT_REDACT_FILE -ErrorAction SilentlyContinue
        Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Add-Step {
    param(
        [string]$Name,
        [int]$ExitCode,
        [int]$DurationMs,
        [string]$Detail
    )
    $safe = Redact-Text $Detail
    $script:Steps += [pscustomobject]@{
        name = $Name
        exit = $ExitCode
        duration_ms = $DurationMs
        detail = $safe
    }
    Write-Host ("::notice::pilot {0} exit={1} duration_ms={2}" -f $Name, $ExitCode, $DurationMs)
    if ($ExitCode -ne 0) {
        $script:Failed = $true
        Write-Host ("::error::pilot {0} exit={1}" -f $Name, $ExitCode)
    }
}

function Invoke-Native {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    $stdout = Join-Path $script:LogDir ($Name + ".out.txt")
    $stderr = Join-Path $script:LogDir ($Name + ".err.txt")
    $watch = [Diagnostics.Stopwatch]::StartNew()
    # The call operator passes each argument as one argv element. Start-Process
    # re-parses a joined string on Windows PowerShell 5.1 and would split a
    # workspace path that contains a space.
    & $FilePath @ArgumentList 1> $stdout 2> $stderr
    $code = 0
    if ($null -ne $LASTEXITCODE) {
        $code = [int]$LASTEXITCODE
    }
    $watch.Stop()
    $errText = ""
    if (Test-Path $stderr) {
        $errText = Get-Content -Path $stderr -Raw -ErrorAction SilentlyContinue
    }
    $safeErr = Redact-Text $errText
    Set-Content -Path $stderr -Value $safeErr -Encoding utf8
    Add-Step -Name $Name -ExitCode $code -DurationMs ([int]$watch.ElapsedMilliseconds) -Detail $safeErr
    return $code
}

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Set-Location $RepoRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$pilotId = [guid]::NewGuid().ToString("n").Substring(0, 8)
$pilotRoot = Join-Path $env:TEMP ("drillintel pilot " + $stamp + "-" + $pilotId)
New-Item -ItemType Directory -Force -Path $pilotRoot | Out-Null
$script:LogDir = Join-Path $pilotRoot "logs"
New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null

if ($AllowInstall) {
    $bootstrap = Resolve-Python $Python
    $venv = Join-Path $pilotRoot "venv"
    & $bootstrap -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "venv creation failed"
    }
    $script:PythonExe = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $script:PythonExe)) {
        throw "AllowInstall did not create Scripts\python.exe"
    }
    & $script:PythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed"
    }
    & $script:PythonExe -m pip install -e $RepoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "editable install failed"
    }
} else {
    $script:PythonExe = Resolve-Python $Python
}

$beside = Join-Path (Split-Path -Parent $script:PythonExe) "drillintel.exe"
if (Test-Path $beside) {
    $script:Drillintel = $beside
} else {
    $found = Get-Command drillintel -ErrorAction SilentlyContinue
    if ($found) {
        $script:Drillintel = $found.Source
    } else {
        throw "drillintel is not installed. Re-run with -AllowInstall from the repository root."
    }
}

$sha = "unavailable"
try {
    $sha = (& git -C $RepoRoot rev-parse HEAD).Trim()
} catch {
    $sha = "unavailable"
}
$pythonVersion = (& $script:PythonExe --version 2>&1 | Out-String).Trim()
$osCaption = [Environment]::OSVersion.VersionString

$workspace = Join-Path $pilotRoot "sample workspace"
$docs = Join-Path $pilotRoot "sample docs"
$html = Join-Path $pilotRoot "sample-report.html"
New-Item -ItemType Directory -Force -Path $docs | Out-Null
@(
    "Well PILOT-1 daily report"
    "Date: 2026-01-15"
    "Mud weight 10.2 ppg"
) | Set-Content -Path (Join-Path $docs "sample.txt") -Encoding utf8

Invoke-Native -Name "version" -FilePath $script:Drillintel -ArgumentList @("--version") | Out-Null
Invoke-Native -Name "workspace-create" -FilePath $script:Drillintel -ArgumentList @("workspace", "create", $workspace, "--name", "Pilot") | Out-Null
Invoke-Native -Name "wells-create" -FilePath $script:Drillintel -ArgumentList @("wells", "create", "--name", "PILOT-1", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "wells-list" -FilePath $script:Drillintel -ArgumentList @("wells", "list", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "doctor" -FilePath $script:Drillintel -ArgumentList @("doctor", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "ingest" -FilePath $script:Drillintel -ArgumentList @("ingest", $docs, "--well", "PILOT-1", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "index-rebuild" -FilePath $script:Drillintel -ArgumentList @("index", "rebuild", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "search" -FilePath $script:Drillintel -ArgumentList @("search", "mud", "--well", "PILOT-1", "--workspace", $workspace) | Out-Null
Invoke-Native -Name "fields-report" -FilePath $script:Drillintel -ArgumentList @("fields", "report", "--well", "PILOT-1", "--workspace", $workspace, "--output", $html) | Out-Null

$reportOk = (Test-Path $html) -and ((Get-Item $html).Length -gt 0)
if (-not $reportOk) {
    Add-Step -Name "fields-report-file" -ExitCode 1 -DurationMs 0 -Detail "HTML report missing or empty"
} else {
    Add-Step -Name "fields-report-file" -ExitCode 0 -DurationMs 0 -Detail "HTML report is non-empty"
}

$listOut = Join-Path $script:LogDir "wells-list.out.txt"
$listText = ""
if (Test-Path $listOut) {
    $listText = Get-Content -Path $listOut -Raw -ErrorAction SilentlyContinue
}
if ($listText -notmatch "PILOT-1") {
    Add-Step -Name "wells-list-name" -ExitCode 1 -DurationMs 0 -Detail "well list did not contain the created name"
} else {
    Add-Step -Name "wells-list-name" -ExitCode 0 -DurationMs 0 -Detail "well list contained the created name"
}

if ($AllowProviderProbe) {
    Invoke-Native -Name "ai-status" -FilePath $script:Drillintel -ArgumentList @("ai", "status", "--workspace", $workspace) | Out-Null
}

if ($ReleaseSmoke) {
    Invoke-Native -Name "release-smoke" -FilePath $script:PythonExe -ArgumentList @((Join-Path $RepoRoot "tools\release_smoke.py")) | Out-Null
}

if ($OffscreenUi) {
    $env:QT_QPA_PLATFORM = "offscreen"
    $env:QT_OPENGL = "software"
    Invoke-Native -Name "offscreen-ui" -FilePath $script:PythonExe -ArgumentList @("-m", "pytest", (Join-Path $RepoRoot "tests\ui"), "-q", "--tb=line") | Out-Null
}

if ($FullPytest) {
    $timeoutMs = [Math]::Max(1, $FullPytestTimeoutMinutes) * 60 * 1000
    $stdout = Join-Path $script:LogDir "full-pytest.out.txt"
    $stderr = Join-Path $script:LogDir "full-pytest.err.txt"
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $script:PythonExe -ArgumentList @("-m", "pytest", "-q", "--tb=line") -PassThru -NoNewWindow -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $finished = $proc.WaitForExit($timeoutMs)
    if (-not $finished) {
        try { $proc.Kill() } catch { }
        $watch.Stop()
        Add-Step -Name "full-pytest" -ExitCode 124 -DurationMs ([int]$watch.ElapsedMilliseconds) -Detail "interrupted by the pilot timeout"
    } else {
        $watch.Stop()
        $errText = ""
        if (Test-Path $stderr) { $errText = Get-Content -Path $stderr -Raw -ErrorAction SilentlyContinue }
        Add-Step -Name "full-pytest" -ExitCode ([int]$proc.ExitCode) -DurationMs ([int]$watch.ElapsedMilliseconds) -Detail (Redact-Text $errText)
    }
}

$summary = [ordered]@{
    manual_window_checklist = "NOT RUN"
    opened_user_window = $false
    provider_probe = [bool]$AllowProviderProbe
    os = $osCaption
    powershell = $PSVersionTable.PSVersion.ToString()
    python = $pythonVersion
    sha = $sha
    workspace = "disposable-temp"
    steps = $script:Steps
    failed = [bool]$script:Failed
}
$jsonPath = Join-Path $pilotRoot "pilot-summary.json"
$mdPath = Join-Path $pilotRoot "pilot-summary.md"
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $jsonPath -Encoding utf8
$lines = @(
    "# Windows pilot summary",
    "",
    "Manual window checklist: NOT RUN. This script did not open a user-visible window.",
    "Provider probe: $(if ($AllowProviderProbe) { 'requested' } else { 'not requested' }).",
    "SHA: $sha",
    "Python: $pythonVersion",
    "PowerShell: $($PSVersionTable.PSVersion)",
    "OS: $osCaption",
    ""
)
foreach ($step in $script:Steps) {
    $lines += "- $($step.name): exit $($step.exit), $($step.duration_ms) ms"
}
$lines += ""
$lines += "Summary JSON: pilot-summary.json in the disposable directory."
$lines | Set-Content -Path $mdPath -Encoding utf8
Write-PilotNote ("pilot summary: " + $mdPath)
if ($script:Failed) {
    exit 1
}
exit 0
