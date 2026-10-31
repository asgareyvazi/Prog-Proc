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
and measures it. The same experiment runs here:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe tools\qt_shutdown_stress.py --attempts 25 --log-dir qt-logs\shutdown-stress --gate
```

> **Correction (V8.17).** This section used to say that a native access
> violation was reported at error level and did **not** fail the job,
> because a fault that fires in a few percent of attempts turns a required
> job into a coin toss. That was the policy of an open defect. The defect
> is closed by construction - the Windows termination path can no longer
> reach `ExitProcess` - so a recurrence is a regression, and the step is
> gated: the CI runs the same command with `--gate` and a single fault
> fails the job. Every attempt is still reported, and the artifact is
> uploaded whether the step passes or fails.

Every attempt must report exit `4`. An attempt that reports
`3221225477` (`0xC0000005`) is a native access violation, and it is a
failure of this workstation, not a flaky test. Record the number of
attempts and every failure; do not re-run until one attempt passes.

**What this experiment measured.** The native access violation was
reproduced on the Windows runner — at least ten faults in fifty attempts
at commit `eab9323`. The cause was the interpreter finalizer: on Windows
the terminal exit used `os._exit`, which is `ExitProcess`, which notifies
the loaded libraries and runs the finalizer while the `QApplication` and a
live GUI thread still exist. The finalizer destroyed them under the GUI
thread. Since commit `2827073` the Windows branch of the terminal exit
cannot reach `os._exit` at all, and the same fifty attempts published
**zero** native faults.

> **Correction (V8.17, ADR-61).** "The Python finalizer" names a
> mechanism that does not exist. On Windows `os._exit` is the CRT
> `_exit` → `ExitProcess`, which notifies the loaded DLLs
> (`DLL_PROCESS_DETACH`) and runs the native Qt/PySide6/shiboken
> teardown while the GUI thread is inside a Python callback; it does
> **not** run Python interpreter finalization. The access violation is
> real and is not withdrawn — only the label is corrected.

If an attempt still reports `3221225477` on your workstation, record the
whole line: it carries the stage the child reached and the innermost frames
of the thread that faulted. Those frames are the evidence, and they are
what a fix has to be based on.

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

> **Correction (V8.17, ADR-61).** "The Python finalizer" names a
> mechanism that does not exist. On Windows `os._exit` is the CRT
> `_exit` → `ExitProcess`, which notifies the loaded DLLs
> (`DLL_PROCESS_DETACH`) and runs the native Qt/PySide6/shiboken
> teardown while the GUI thread is inside a Python callback; it does
> **not** run Python interpreter finalization. The access violation is
> real and is not withdrawn — only the label is corrected.

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
Opening the review without confirming an action is read-only: `DomainReviewService` does not mutate the workspace. Explicit, confirmed actions through `ReviewActionService` (`src/drilling_intelligence/review/actions.py`) are the governed mutation boundary — they are submitted as an immutable request with expected preconditions, executed outside the GUI thread by `ReviewActionWorker`, and re-read into a fresh review on success. Promotion of a new domain row is still `records promote`, not a review click.

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

**Status: NOT RUN.** This list has never been executed on the owner's
workstation. Nothing on this page completes a box, and no automated run -
including the green `windows-2022` jobs - completes one either. A box is
ticked by the person who ran that step on the workstation, in the record
below, and by nobody else.

### Before the first step: the acceptance record

Write these down before running anything. Without them, a result cannot be
compared with any other run, and a failure cannot be reproduced.

| What to record | How |
| --- | --- |
| Windows edition and build | `Get-ComputerInfo` \| Select-Object WindowsProductName, OsBuildNumber, OsVersion |
| Architecture and RAM | `$env:PROCESSOR_ARCHITECTURE`; `(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory` |
| CPython | `.\.venv\Scripts\python.exe -VV` |
| Repository SHA | `git rev-parse HEAD` **and** `git status --porcelain=v2 --branch` |
| Branch and cleanliness | the two lines above must agree that the tree is clean |
| Installed Pin set used | `git rev-parse HEAD:constraints/development.txt` |
| Qt and PySide6 | `.\.venv\Scripts\python.exe -c "import PySide6; from PySide6.QtCore import qVersion; print(PySide6.__version__, qVersion())"` |
| Qt platform | `.\.venv\Scripts\python.exe -c "from PySide6.QtWidgets import QApplication; print(QApplication([]).platformName())"` |
| The disposable workspace path | a new folder under `$env:TEMP` whose name you can delete |
| Date and who ran it | the person, not the machine |

Everything below runs against a **disposable** workspace. Do not run it
against an operational record: several steps write to the workspace, and
the point of the exercise is to be able to delete it afterwards.

```powershell
$sha  = git rev-parse HEAD
$ws   = Join-Path $env:TEMP ("drillintel-acceptance-" + $sha.Substring(0,7))
$log  = Join-Path $env:TEMP ("drillintel-acceptance-" + $sha.Substring(0,7) + ".log")
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
"edition/build:" | Out-File $log
Get-ComputerInfo | Select-Object WindowsProductName, OsBuildNumber, OsVersion | Out-File $log -Append
"sha: $sha" | Out-File $log -Append
git status --porcelain=v2 --branch | Out-File $log -Append
```

### The steps

Each step says what to run, what a pass looks like, and what to write in
the record if it fails. `$LASTEXITCODE` is the exit code of the last
native program; check it after every command, because PowerShell will
happily print an error and carry on.

1. **The interpreter is the documented one.**
   `.\.venv\Scripts\python.exe -VV` reports CPython 3.11 or newer,
   and 3.14 is preferred when wheels exist for it. Record the version.
   A Microsoft Store alias that answers `python` is not this step.

2. **The install is the pinned one and exits 0.**
   `.\.venv\Scripts\python.exe -m pip install -c constraints\development.txt -c constraints\build.txt -e ".[dev]"`,
   then `.\.venv\Scripts\python.exe -m pip check`.
   Pass: both exit 0 and `pip check` says no broken requirements.

3. **The tree is the committed one and is still clean.**
   `git status --porcelain=v2 --branch` prints the branch line and the
   SHA and nothing else. Pass: no `1 `, `2 ` or `? ` lines.

4. **The static gates are green on this machine.**
   `-m ruff check .`, `-m ruff format --check .`, `-m mypy`,
   `-m compileall -q src tests migrations tools`, `-m alembic heads`.
   Pass: the first four exit 0, and `alembic heads` prints exactly one
   line containing `(head)`, currently `0019`.

5. **The suite runs, and the skips are named.**
   `.\.venv\Scripts\python.exe -m pytest`.
   Pass: exit 0, and the summary line is copied into the record *including*
   the skip count. Two Windows skips are expected and must be named, not
   counted as passes: the open-database-file move, and the workbook
   handle-count test. A skip that is not one of those two gets its reason
   written down.

6. **The shutdown experiment preserves every attempt.**
   `$env:QT_QPA_PLATFORM = "offscreen"` then
   `.\.venv\Scripts\python.exe tools\qt_shutdown_stress.py --attempts 25 --log-dir qt-logs\shutdown-stress --gate`.
   Pass: 25 attempts, every one exit `4`, and
   `qt-logs\shutdown-stress\summary.json` reports 25 attempts and 0
   failures. Copy the whole `qt-logs\shutdown-stress` folder into the
   record - every attempt log, not only the failures. Do not re-run until
   one attempt passes: an attempt that reports `3221225477`
   (`0xC0000005`) is a failure of this workstation, and the frames it
   prints are the evidence a fix has to be built on.

7. **The console commands answer.**
   `.\.venv\Scripts\drillintel.exe --help` and
   `.\.venv\Scripts\drillintel.exe --version`. Pass: exit 0 for
   both, and the version line names the interpreter.

8. **A workspace and a well can be created on a path with a space.**
   The path in `$ws` above has no space by accident - the acceptance path
   also runs with one:
   `$ws2 = Join-Path $env:TEMP "drillintel acceptance space"`,
   `drillintel.exe workspace create $ws2 --name Acceptance`,
   `drillintel.exe wells create --name "SAMPLE-1" --workspace $ws2`,
   `drillintel.exe wells list --workspace $ws2`.
   Pass: the well is listed. Record the exact paths used.

9. **`doctor` stays offline.**
   `drillintel.exe doctor --workspace $ws2` and
   `drillintel.exe --json doctor --workspace $ws2`.
   Pass: it exits 0 or 1, and the JSON parses. Watch the machine's network
   indicator, or run it with the adapter disabled: nothing here may open a
   connection. A provider finding is configuration, not availability.

10. **A synthetic document ingests and extracts without MinerU.**
    Create `sample.txt` as shown in "A small synthetic ingest" above, then
    `drillintel.exe ingest $docs --well "SAMPLE-1" --workspace $ws2`.
    Pass: the summary names the file as ingested, and no step fails because
    MinerU is absent. A scanned page demanding OCR is expected to be
    reported as needing MinerU, not to crash.

11. **The index rebuilds and the search returns something.**
    `drillintel.exe index rebuild --workspace $ws2` and
    `drillintel.exe search mud --well "SAMPLE-1" --workspace $ws2`.
    Pass: the rebuild exits 0 and the search finds the synthetic line.

12. **The HTML report opens in a browser.**
    `drillintel.exe fields report --workspace $ws2 --well "SAMPLE-1" --output (Join-Path $env:TEMP "sample-report.html")`.
    Pass: `$LASTEXITCODE` is 0, the file opens, the identity shown in the
    page matches the JSON identity, and the page needs no network: no
    script tag, no `https://` reference.

13. **The workbench opens and closes, repeatedly.**
    `$env:QT_QPA_PLATFORM` **unset** for a real window; then
    `.\.venv\Scripts\drillintel-ui.exe --workspace $ws2 --well "SAMPLE-1"`.
    Close it from its own close control, and repeat five times.
    Pass: every launch opens a window, every close ends the process with
    exit 0, and no `0xC0000005` appears in the console or in the Windows
    application event log. Record the number of launches and the exit code
    of each one.

14. **The review surface is read-only until an action is explicitly confirmed.**
    In the workbench, open the review surface and browse without confirming any action. Then
    `drillintel.exe records list --workspace $ws2 --well "SAMPLE-1"` and
    compare with the same command before the window was opened.
    Pass: the rows are identical — passive review does not mutate. A confirmed action through `ReviewActionService` does mutate via its owning domain repository (it is not a silent promotion), reloads a fresh `DomainReview` on success, and leaves the display unchanged on failure. The prompt must derive its actions from `available_actions()`, require actor/reason/evidence according to the lifecycle, block concurrent submissions, and run outside the GUI thread — see `docs/HUMAN_REVIEW_ACTION_INVENTORY.md` and `src/drilling_intelligence/ui/worker.py` `ReviewActionWorker`.

15. **Non-ASCII text survives the console.**
    `.\.venv\Scripts\drillintel.exe wells create --name "Ø-test" --workspace $ws2`
    then `wells list`. Pass: the name reads back exactly, with no `?` and no
    `UnicodeEncodeError`. `$env:PYTHONUTF8` must be `1` for this; record
    whether it was set.

16. **The logs carry no document body and no credential.**
    Open `$ws2\.drillintel\logs\`. Pass: no file contains the text of
    `sample.txt`, no API key, no connection string. If one does, that is a
    security defect: stop, keep the file, and report it without pasting the
    secret.

### The record

Fill this in as you go; leave a box unticked until the step has run **on
this workstation**. "Skipped" is a result, and it is written down with its
reason - it is never a tick.

| # | step | result | evidence |
| --- | --- | --- | --- |
| 1 | interpreter version | NOT RUN | |
| 2 | pinned install and `pip check` | NOT RUN | |
| 3 | tree clean at the recorded SHA | NOT RUN | |
| 4 | ruff, format, mypy, compileall, alembic head | NOT RUN | |
| 5 | suite, with named skips | NOT RUN | |
| 6 | shutdown stress, 25 attempts, every log | NOT RUN | |
| 7 | `--help` and `--version` | NOT RUN | |
| 8 | workspace and well on a path with a space | NOT RUN | |
| 9 | `doctor` offline | NOT RUN | |
| 10 | synthetic ingest and extraction | NOT RUN | |
| 11 | index rebuild and search | NOT RUN | |
| 12 | HTML report in a browser | NOT RUN | |
| 13 | workbench open and close, five times | NOT RUN | |
| 14 | review controls write nothing | NOT RUN | |
| 15 | non-ASCII console round trip | NOT RUN | |
| 16 | logs sanitized | NOT RUN | |

Until every row says otherwise, the desktop acceptance state of this
project is **NOT RUN**, and the reports say so.
