# Windows testing

This is the operator route for a Windows 10 or Windows 11 workstation. It is
not a claim that Windows is fully supported. Automated coverage is the
`windows-2022` workflow in `.github/workflows/ci-windows.yml`. That image is
not this workstation. The workflow runs CPython 3.11 and 3.14 for the core
suite, and CPython 3.11 for clean-install smoke, the disposable pilot, and
the offscreen UI job. Exact totals for a SHA are in
`docs/mission-reports/V8.13_FINAL_REPORT.md` and
`docs/mission-reports/V8.14_FINAL_REPORT.md`. A green offscreen job is not
the same as opening the workbench on this machine.

Commands below are PowerShell. Do not paste the Bash examples from the
README. Keep the repository line endings as LF (`git config core.autocrlf
false`) so format checks match the committed files.

The default install does not download a model and does not contact Ollama.
`drillintel doctor` stays offline. Do not start a provider probe unless you
mean to open a network connection.

Use a disposable workspace. Do not point these commands at an operational
record you cannot replace.

## What you need

- Windows 10 or Windows 11, 64-bit.
- CPython 3.11 or newer. 3.11 is the floor. 3.14 is the product target when
  wheels install. The Windows UI job uses 3.11.
- PowerShell 5.1 or PowerShell 7. Set UTF-8 so console text matches the
  tests:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python --version
```

If `python` is the Microsoft Store alias, install Python from python.org and
open a new terminal. `python3` is not required; use `python`.

## Editable install and tests

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c constraints\development.txt -c constraints\build.txt -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m compileall -q src tests migrations tools
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m pytest
```

The constraints pin the versions the CI proves. Drop them for a plain
install; keep them when you want the machine to match the recorded run.
`python -m mypy` checks one package boundary, `drilling_intelligence.core`.

`alembic heads` must show one line containing `(head)`, currently `0019`.
UI tests skip when PySide6 is not installed. That skip is not a pass. The
reason is `QtWidgets unavailable`.

A path with spaces is valid if every program is invoked by its full path,
not through a shell string:

```powershell
$root = Join-Path $env:TEMP "drillintel venv"
python -m venv $root
& (Join-Path $root "Scripts\python.exe") -m pip install -e ".[dev]"
```

## Shutdown stress on this workstation

The Windows offscreen UI job runs the fail-closed shutdown child repeatedly
and fails the job if any attempt ends with anything other than exit `4`.
The same experiment runs here:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe tools\qt_shutdown_stress.py --attempts 25 --log-dir qt-logs\shutdown-stress
```

Every attempt must report exit `4`. An attempt that reports
`3221225477` (`0xC0000005`) is a native access violation, and it is a
failure of this workstation, not a flaky test. Record the number of
attempts and every failure; do not re-run until one attempt passes.

## Release smoke

This builds the wheel and the sdist, installs each outside the checkout, and
drives the installed `drillintel.exe`:

```powershell
.\.venv\Scripts\python.exe tools\release_smoke.py
```

The script looks for `Scripts\python.exe`, `Scripts\pip.exe`, and
`Scripts\drillintel.exe`. A missing suffix is a failure, not a guess.

## Disposable pilot

From the repository root, after the editable install above, this runs the
console checks in a unique temp directory whose name contains a space. It
does not install anything unless `-AllowInstall` is passed, and it does not
contact Ollama unless `-AllowProviderProbe` is passed. It writes
`pilot-summary.md` and `pilot-summary.json` in that directory. The manual
checklist below stays unchecked.

```powershell
.\tools\windows_pilot.ps1 -Python .\.venv\Scripts\python.exe
```

Optional switches, each explicit: `-AllowInstall`, `-ReleaseSmoke`,
`-FullPytest`, `-OffscreenUi`, `-AllowProviderProbe`. `-OffscreenUi` is still
not a visible window. `-FullPytest` can be interrupted; an interrupted suite
is not a pass.

If a step fails, send the summary, the Windows edition, `python --version`,
the failed command, `$LASTEXITCODE`, and the redacted error line. Do not
paste document bodies, environment dumps, or connection strings.

## Known automated skip

Windows core CI skips one test whose reason is `this platform will not move
an open database file`. Windows will not rename a SQLite file that still has
a connection. That skip is not a pass. The supported move is to close the
workspace, then move the folder, then open it at the new path. A separate
test renames the open database file itself and checks that a refusal leaves
the original file and does not create the destination.

The workbook handle-count test also skips on Windows. The reason is
`Windows process handle count is not a file-descriptor leak measurement`.
Two runs grew by 3 and by 11, so that counter cannot prove the archive
bound. The skip is not a pass. Linux still counts `/proc/self/fd`.

The offscreen UI job has also seen the fail-closed shutdown child exit
`3221225477` (`0xC0000005`) instead of 4. The cause is now stated: two
threads could reach the same shutdown expiry and both tried to end the
process, and the coordinator's `ExitProcess` ran the Python finalizer while
the `QApplication` and a live `QThread` still existed. The shutdown now has
one owner, and the loser stands down. A later green UI job still does not
prove that access violation cannot recur. It is not a pass of that case.
The shutdown stress step above is the measurement that would show it.

## Optional workbench

Install Qt only if you want the desktop window:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui]"
```

Create a disposable workspace and a synthetic well. The example uses the
temp directory, not a production folder:

```powershell
$ws = Join-Path $env:TEMP "drillintel-sample"
.\.venv\Scripts\drillintel.exe workspace create $ws --name "Sample"
.\.venv\Scripts\drillintel.exe wells create --name "SAMPLE-1" --workspace $ws
.\.venv\Scripts\drillintel.exe wells list --workspace $ws
.\.venv\Scripts\drillintel.exe doctor --workspace $ws
.\.venv\Scripts\drillintel-ui.exe --workspace $ws --well "SAMPLE-1"
```

Close the window from its close control. Repeat open and close a few times.
Review actions in the workbench are read-only; they must not create or
promote a record.

Offscreen automated UI tests, not a substitute for that window:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:QT_OPENGL = "software"
.\.venv\Scripts\python.exe -m pytest tests\ui
```

Do not run `xvfb-run` or `tools\qt_workbench.py` on Windows. Those are the
Linux qualification. The Linux 15-attempt stability gate remains required
there and is not replaced by this page.

## A small synthetic ingest

Put a text file in a folder you can delete:

```powershell
$docs = Join-Path $env:TEMP "drillintel-docs"
New-Item -ItemType Directory -Force -Path $docs | Out-Null
@(
  "Well SAMPLE-1 daily report"
  "Date: 2026-01-15"
  "Mud weight 10.2 ppg"
) | Set-Content -Path (Join-Path $docs "sample.txt") -Encoding utf8
.\.venv\Scripts\drillintel.exe ingest $docs --well "SAMPLE-1" --workspace $ws
.\.venv\Scripts\drillintel.exe index rebuild --workspace $ws
.\.venv\Scripts\drillintel.exe search mud --well "SAMPLE-1" --workspace $ws
.\.venv\Scripts\drillintel.exe fields report --workspace $ws --well "SAMPLE-1" --output (Join-Path $env:TEMP "sample-report.html")
```

Open the HTML file in a browser. It is a report of the synthetic workspace,
not an operational recommendation.

## Opt-in Ollama probe

Leave this out unless you have a local Ollama and you intend a network call.
It does not run during install, `doctor`, or workbench startup.

```powershell
.\.venv\Scripts\drillintel.exe ai status --workspace $ws
```

An absent provider must not be described as a successful model. No vector is
stored.

## Where logs and the exit code are

- Workspace log: `$ws\.drillintel\logs\` when file logging is enabled, and
  the console of the terminal that launched the command.
- Exit code of the last native program in PowerShell: `$LASTEXITCODE`.
- Do not paste document text, API keys, or connection strings into a bug
  report. Quote the command, the exit code, and the redacted error line.

## Troubleshooting

| What you see | What it usually means |
|---|---|
| `No module named 'drilling_intelligence'` | The editable install did not run, or a different `python` is first on `PATH`. Use `.\.venv\Scripts\python.exe`. |
| `drillintel-ui could not load Qt` | The `ui` extra is missing, or the Qt platform plugin failed. Install `.[ui]`. This is not an Ollama error. |
| `could not find the Qt platform plugin` | Set `QT_QPA_PLATFORM=offscreen` only for tests. For a real window, leave it unset and install the UI extra. |
| schema or `alembic` errors on a new workspace | `workspace create` should stamp from package metadata. A copied partial `.drillintel` folder is not a new workspace. Delete the disposable folder and create it again. |
| access denied under the temp directory | Choose another empty folder you can write. Do not reuse a workspace another process still has open. |
| MinerU is missing | Expected. Scanned-page OCR is an optional external program. Built-in extractors still run. Do not treat a missing MinerU binary as a failed install. |
| `doctor` mentions the AI provider but does not connect | Expected. Configuration is not availability. |

## Manual acceptance checklist

Run this on the workstation you care about. Record the Windows edition,
build if you have it, `python --version`, the repository SHA, and
`$LASTEXITCODE` for each failed step. A skipped step is not a pass; write
the skip reason. Until those notes exist, this checklist is unexecuted.
The disposable pilot does not complete these boxes.

- [ ] `python --version` is 3.11 or newer.
- [ ] `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` exits 0.
- [ ] `tools\qt_shutdown_stress.py --attempts 25` reports exit `4` for every attempt.
- [ ] `drillintel.exe --help` and `--version` exit 0.
- [ ] Workspace create and open on a temp path, including a path with a space.
- [ ] Well create and well list show the well you created.
- [ ] `doctor` exits without opening a network connection.
- [ ] A synthetic text file ingests and extraction does not require MinerU.
- [ ] Search and `index rebuild` run on that workspace.
- [ ] `fields report` writes HTML you can open.
- [ ] `drillintel-ui.exe` opens and closes repeatedly on that workspace.
- [ ] Review controls do not write a new engineering record.
- [ ] Console text with a non-ASCII character is readable (`PYTHONUTF8=1`).
- [ ] Logs were captured without document bodies or credentials.
