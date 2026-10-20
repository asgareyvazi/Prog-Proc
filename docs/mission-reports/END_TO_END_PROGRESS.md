# PROG-PROC end-to-end mission — progress ledger

**Status:** active
**Opened:** 2026-10-20
**Repository:** `asgareyvazi/Prog-Proc`
**Session branch:** `arena/5957b76d-prog-proc`
**Baseline SHA:** `317c55365319af61138000664293ae2d84e27a6e` (V8.15, the tip of `arena/01a0c936-prog-proc`)

This file is the running ledger of the mission. It is written as the work
happens, so a reader can see the order of the findings and not only the
conclusion. The final report is
[`END_TO_END_FINAL_REPORT.md`](END_TO_END_FINAL_REPORT.md). The historical
reports in this directory are not edited; this mission adds dated follow-ups.

**Verdict rules used throughout.** `DONE` only if every part of the scope is
complete, pushed, the exact-final-SHA CI is green on Linux and Windows, and
no critical defect remains in the claimed scope. `PARTIAL` if the Windows
access violation is still unexplained or unresolved, a material test still
fails, evidence is missing, the owner checklist is NOT RUN, or owner
decisions remain open. `BLOCKED` if a material blocker prevents safe
implementation or publication. No percentage of completion is claimed.

---

## 1. Reconstructing the true repository state

| Check | Result |
| --- | --- |
| clone | shallow/grafted: `git log` showed one `(grafted)` commit |
| recovery | `git fetch --unshallow origin` — 212 commits recovered |
| tip chain | `317c553` (V8.15) -> `ef6a727` -> `2e1f5e6` -> `69c4bd4` -> `13ab9bd` -> `8566532` (V8.12) -> `7fe7a4d` (V8.11) |
| `git ls-remote origin` | `main` = `85122e1a…`; `arena/01a0c936-prog-proc` = `317c553…` |
| worktree at start | clean, 336 tracked files |
| Alembic head | `0019 (head)` |

**Branch conflict, disclosed.** The task named `arena/01a0c936-prog-proc`;
the platform rule for this session says the work belongs on
`arena/5957b76d-prog-proc` and nowhere else. The platform branch was used
for every commit and every push. Nothing was pushed to the other branch, no
force-push was used, and no history was rewritten.

**Baseline CI, re-read before any edit.**

| Run | SHA | Result |
| --- | --- | --- |
| Linux `39208929562` | `317c553` | success |
| Windows `39208929500` | `317c553` | **failure** — `windows ui (offscreen Qt)`, job `117917753945` |

---

## 2. The Windows access violation

### 2.1 What the failing job actually said

The log zip could not be downloaded from this environment (the artifact host
is outside the reachable allowlist and every request ended in `EOF`). The
check-run annotations were readable and are the evidence.

| Field | Value |
| --- | --- |
| child exit | `3221225477` (`0xC0000005`) — not the contracted `4` |
| newest Python frame | `lifetime.py:426 finish_qt_application` |
| then | `lifetime.py:507 application_shutdown` |
| then | `qt_shutdown_child.py:443`, mode `production_deadline` |
| then | `main:761` -> `<module>:765` |
| markers | `REAL_QTHREAD, LOOP_STARTED, QUIT_REQUESTED, ABOUT_TO_QUIT_WINDOW_VALID, EXEC_RETURNED` |
| suite | 22 collected, 21 passed, 1 failed, 0 skipped, 53.271 s |

The failing mode is the **real `QThread`** mode, not the synthetic one.
A previous session's note that the synthetic mode was the crashing one is
corrected here by the annotation itself.

### 2.2 The cause

Two threads reach the same shutdown expiry.

* The coordinator polls the release budget and calls `fail_closed_shutdown`.
* The deadline monitor guarantees the ceiling and calls `_terminal_exit`.

Both then tried to end the process. The coordinator used `os._exit`, which
is `ExitProcess` on Windows; `ExitProcess` notifies the loaded libraries, so
the Python finalizer runs while the `QApplication` and a live `QThread` still
exist. The monitor used `TerminateProcess`, which does not notify the
libraries. The monitor's Windows path waits up to 50 ms for the deadline
notice, and that wait is the window in which the two overlapped.

**Proof that the double end is reachable, not a theory.** The implementation
at `317c553` was loaded directly and the coordinator and the monitor were
raced into the same expiry: **two** `exit(4)` calls were recorded. The
pre-fix local stress run also showed both owners (`owner=monitor` 1 of 8,
`owner=coordinator` 7 of 8). The regression test added in this mission
fails against that implementation.

### 2.3 The fix

Process-wide single-owner arbitration in
`src/drilling_intelligence/ui/lifetime.py`:

* one arbiter owns the terminal exit; the first thread to claim it ends the
  process;
* the loser stands down — it does not terminate and does not return into Qt
  or into interpreter teardown (bounded 3 s wait, then the same end-of-process
  path as a backstop);
* the coordinator now ends the process the way the monitor does: flush the
  standard streams, then `TerminateProcess` on Windows and `os._exit`
  elsewhere. The Python finalizer no longer runs while Qt objects are alive.

`is_fail_closed_shutdown_exit` is unchanged: it accepts only the integer
`4`, and still rejects `3221225477`, `-1073741819`, `True` and `0`. No
assertion was weakened, no test was skipped, no test was `xfail`ed.

### 2.4 Reproduction status — REPRODUCED

**The native access violation is reproduced.** The Windows offscreen UI job
at `32483ed` ran the shutdown stress experiment with 25 attempts in each of
two modes, and **four of the fifty attempts** ended in `0xC0000005`:

| mode | attempts | native access violations |
| --- | --- | --- |
| `deadline` (real `QThread`) | 25 | 2 — attempts 21 and 25 |
| `deadline-synthetic` (`threading.Thread`) | 25 | 2 — attempts 5 and 23 |

That is about **8% per attempt**. The UI suite in the same job was green
(22 collected, 22 passed, 0 failed, 0 skipped).

**What this changes.** The double terminal end is a real defect: it was
reproduced from the source (two `exit(4)` calls in one race) and it is
fixed. It is **not the whole cause**. With the arbitration in place the
fault still occurs at about 8%, so the earlier reading — that the double end
*was* the cause — is withdrawn. The remaining cause is not established.

**What the two modes tell us.** The `deadline` mode runs a real `QThread`;
the `deadline-synthetic` mode runs a plain `threading.Thread`. They fault
at the same rate, so a live `QThread` is not the cause. What they share is
the shutdown path.

**What is missing, and what the next run showed.** The run that reproduced
the fault did not publish the transcript of a faulted child. The next
Windows run, at `35b0552`, failed in the UI suite itself and did carry the
transcript — and it says something important: the child reached
`SHUTDOWN_BEGIN` and faulted **before any terminal decision was recorded**
(no `TERMINAL_ATTEMPT` line). The fault is therefore **not in the
termination path**; it is on the GUI thread inside the coordinator's
release loop, before the shutdown gives up.

The failure message keeps the last 500 characters of the child output, and
a `faulthandler` stack is printed innermost-frame-first, so the cut dropped
the inner frames — the fault location. The UI test and the stress harness
now put the innermost frames first, so the next Windows run names the file
and the line.

**The measurement is not a gate** (ADR-59). The known native fault is
measured and published; it does not fail the required job, because a fault
that fires in a few percent of attempts turns a required job into a coin
toss (ADR-55 reached the same conclusion). Any attempt that ends with an
exit code other than `4` and other than the known native fault still fails
the step, and the UI suite's single deadline child is still required to
exit `4`.

---

## 3. Commits on this branch

| SHA | Subject |
| --- | --- |
| `1299b59b00cd605a3141a718aa1af969a7c3c10b` | V8.16: instrument the fail-closed shutdown and measure its failure rate |
| `1ec8a1ba219521abbfef9f73e3ee6177f053cd19` | V8.16: end the Qt shutdown exactly once, and pin the dependency set |
| `91e51a49bc334c81f059b60bdc479bb3f0e00756` | V8.16: type-check the core boundary, measure coverage, pin the actions |
| `32483ed89c06981d7e8fdfb85639ec6ab129ca31` | V8.16: publish the coverage baseline where it can be read |
| (this commit) | V8.16: measure the native fault, publish the stage a faulted child reached |

Each was pushed to `origin/arena/5957b76d-prog-proc` and verified with
`git ls-remote`. No commit was amended, no history was rewritten.

---

## 4. Audit findings, in the order they were made

| Area | Finding | Action |
| --- | --- | --- |
| Qt shutdown | two concurrent terminal exits | fixed, with a regression test that fails on the old code |
| supply chain | no version record of what is installed; no vulnerability scan; actions referenced by tag | `constraints/`, `tools/dependency_audit.py`, actions pinned to commit SHAs |
| type safety | no type checker anywhere | one strict boundary (`core`), four real defects fixed |
| coverage | never measured | measured and published; no invented threshold |
| domain coverage | `docs/DOCUMENT_DOMAIN_COVERAGE.md` re-counted against the source | **accurate as written** — 26 enum members, 26 contracts, 13 `END_TO_END_CERTIFIED`, 8 `KNOWLEDGE_SUPPORTED`, 5 `EXTRACT_ONLY`, 11 handlers, 21 taxonomy signatures |
| migrations | 19 revisions, one head | unchanged, `0019` |
| packaging | wheel and sdist build, install clean, CLI works | re-verified by `tools/release_smoke.py` |
| broad exceptions | 86 `except Exception` sites | inspected; each is at a documented boundary. No blanket swallow was added or removed |
| large functions | `review/service.py::_build` (742), `cli/app.py::build_parser` (729), `command_records` (498) | recorded as a finding; not refactored, because a refactor of that size without a baseline is the risk this mission was told to avoid |
| Windows skips | two named skips | kept as skips, with their honest reasons |
| owner checklist | manual Windows checklist | **NOT RUN** — it is the owner's to run |
| product decisions | 6 open items in `docs/PRODUCT_DECISIONS_REQUIRED.md` | left open; no capability was invented |

---

## 5. Local evidence

| Check | Result |
| --- | --- |
| `ruff check .` | clean, 333 files |
| `ruff format --check .` | clean |
| `python -m mypy` | clean, 13 files in `drilling_intelligence.core` |
| `python -m compileall -q src tests migrations tools` | clean |
| `alembic heads` | `0019 (head)` |
| `tools/release_smoke.py` | PASSED — wheel and sdist, 49 tables, schema stamped from package metadata |
| `tools/dependency_audit.py` | `clean` — no known vulnerability in the pinned runtime and UI sets |
| `tests/unit/` | all passed |
| `tests/ui/` (offscreen, local stub libraries) | 22 passed |
| shutdown stress, post-fix | 12/12 attempts exit `4`, 0 failures, owners `{coordinator: 10, monitor: 2}` |
| `tools/constraints.py --set <name>` | regenerates each constraint file; `--check` fails on a hand edit |

### One flake, recorded honestly

`tests/ui/test_workbench.py::test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail`
failed **once** in about fourteen local runs of `tests/ui/`, and passed in the
other thirteen. It passes in isolation every time. The repository already
knows this node is fragile: it is one of the two nodes in the 15-attempt
stability gate (`tools/qt_stability.py`), and a later pass never erases an
earlier failure there. It was not reproduced in eight further consecutive
runs. No test was weakened, skipped or retried into green. The local
environment is not the CI environment: it runs against stubbed graphics
libraries (`/tmp/qtstub/lib`), because the sandbox cannot reach the Debian
package mirror.

---

## 6. What is still open

* The Windows native access violation **is reproduced**, at about 8% per
  attempt. The double terminal end is fixed, but the remaining cause is not
  established, and the shutdown path was not restructured on a guess.
* The owner's manual Windows checklist is **NOT RUN**.
* Six product decisions remain open in `docs/PRODUCT_DECISIONS_REQUIRED.md`.
  No capability was added for any of them.
* The type-checking boundary covers `drilling_intelligence.core` only. The
  remaining packages are not checked.
* The Windows artifact bytes (job logs, JUnit, Qt evidence archive) could
  not be downloaded from this environment. The job conclusions and the
  check-run annotations were readable and are what the reports cite.
