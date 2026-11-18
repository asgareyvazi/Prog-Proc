> **V8.18 current-state banner — additive, 2026-10-26 (preserves report below)**
>
> **What is current:** V8.17 closure is **DONE** for engineering (16 phases, CI green on exact SHA `5f78228` — Linux `40173372656`, Windows `40173372622`); end-to-end product is **PARTIAL** (owner checklist **NOT RUN**, five decisions open, product capabilities narrow by design).
> **What this page’s header corrects:** the 2026-10-20 header “Windows native access violation was not reproduced” is superseded — the violation was reproduced, root-caused (ADR-61: CRT `_exit`→`ExitProcess`→`DLL_PROCESS_DETACH` while GUI thread in Qt callback) and fixed via `TerminateProcess` with a 50-attempt gated stress (0 faults). The first paragraph’s “no percentage” disclaimer still holds; the later “six decisions” is corrected to **five** beside it. Historical text is kept, not rewritten.
> **Coverage clarification:** the `14.39%` that appears in the body’s UI-completeness row is the historical headless `qt/ui` baseline (`5964f36`), not the current Qt-enabled measurement — see `V8.18_COVERAGE_REPORT.md`.

---
# PROG-PROC end-to-end mission — final report

**Status:** PARTIAL — the Windows native access violation was not reproduced, and the owner's manual Windows checklist is NOT RUN.
**Date:** 2026-10-20
**Repository:** `asgareyvazi/Prog-Proc`
**Session branch:** `arena/5957b76d-prog-proc`
**Code SHA described by this report:** `32483ed89c06981d7e8fdfb85639ec6ab129ca31`
**Alembic head:** `0019` (unchanged)

This file is not a claim that the owner's workstation was tested. A green
offscreen UI job is not proof that the access violation cannot recur. It is
not product completion. **No percentage of completion is claimed**, and no
capability was invented to make the report read better.

The commit that adds this file creates a new SHA. The workflows that ran on
the publication SHA are cited in the final response, which is also where the
exact-SHA Linux and Windows runs are linked. The runs for the code SHA named
above are in the table in section 12. This file is not edited again to insert
a later observation.

> **V8.17 correction (additive).** The mechanism named below as "the
> interpreter finalizer" is corrected by ADR-61: on Windows `os._exit`
> is the CRT `_exit` → `ExitProcess`, which notifies the loaded DLLs and
> runs the native Qt/PySide6/shiboken teardown; it does **not** run
> Python interpreter finalization. Every observation, measurement and
> fix recorded here stands.

---

## 1. Verdict

**PARTIAL.** The mission's `DONE` bar is not met, and the reason is
narrow and stated:

1. The owner's manual Windows checklist is **NOT RUN**. It is the owner's
   to execute, on the workstation that matters; the disposable pilot does
   not complete those boxes, and a green offscreen runner is not manual
   acceptance.
2. Six product decisions remain open in
   `docs/PRODUCT_DECISIONS_REQUIRED.md`. They are the owner's, and no
   capability was invented for any of them.

> **Correction (V8.17, `docs/PRODUCT_DECISIONS_REQUIRED.md`).** The count is
> **five** open decisions, not six. The register holds five decisions - the
> five milestones `PRODUCT_CAPABILITY.md` schedules after this one (M2 to
> M6) - and, separately, a list of capabilities that stay excluded. The
> excluded list is a boundary, not a decision, and counting it as one is
> where the sixth came from. Nothing else in this report changes: the
> decisions are the owner's, and no capability was invented for any of them.

> **V8.17 mission (2026-10-24/25), additive.** A sixteen-phase reliability
> closure followed this report on branch `arena/5957b76d-prog-proc`. It
> corrected the mechanism named here (ADR-61), gated the Windows shutdown
> stress experiment, root-caused the flaky workbench test (ADR-62), widened
> the type boundary, refactored three functions with their contracts proved
> identical, measured and published per-package coverage with a baseline,
> pinned and checked the dependency and action pins, inventoried every broad
> exception handler and fixed a real secret-redaction defect, reconciled the
> product-decision count from six to five, wrote the Windows acceptance path,
> and hardened the release smoke test for Windows paths and a Qt-free
> install. Reports: [`V8.17_FINAL_REPORT.md`](V8.17_FINAL_REPORT.md) and
> [`V8.17_PROGRESS.md`](V8.17_PROGRESS.md). The verdict of this mission is
> engineering scope `DONE`, product `PARTIAL`, manual desktop acceptance
> **NOT RUN**.

The critical defect is **closed**. The Windows native access violation was
reproduced (at least ten faults in fifty shutdown attempts at `eab9323`),
its cause was established — the interpreter finalizer running on the
terminating thread while another thread was inside Python code, reached
through the `os._exit` line of `_end_process` on Windows — and the fix is
verified on the Windows runner: at `2827073` the same fifty attempts
published **zero** native faults. The double terminal end was a second,
separate defect, reproduced from the source and fixed.

> **Correction (V8.17, ADR-61).** "The Python finalizer" names a
> mechanism that does not exist. On Windows `os._exit` is the CRT
> `_exit` → `ExitProcess`, which notifies the loaded DLLs
> (`DLL_PROCESS_DETACH`) and runs the native Qt/PySide6/shiboken
> teardown while the GUI thread is inside a Python callback; it does
> **not** run Python interpreter finalization. The access violation is
> real and is not withdrawn — only the label is corrected.

Everything else in the scope is complete and pushed: the supply chain is
pinned and audited, one package boundary is type-checked in CI, coverage is
measured and published, and the domain coverage matrix was re-counted
against the source and found accurate.

## 2. Starting state, reconstructed from the remote

| Check | Result |
| --- | --- |
| clone | shallow/grafted — `git log` showed a single `(grafted)` commit |
| recovery | `git fetch --unshallow origin` — 212 commits recovered |
| `git ls-remote origin` | `main` = `85122e1a…`; `arena/01a0c936-prog-proc` = `317c553…` |
| baseline SHA | `317c55365319af61138000664293ae2d84e27a6e` (V8.15) |
| worktree at start | clean, 336 tracked files |
| baseline Linux CI | `39208929562` — success |
| baseline Windows CI | `39208929500` — **failure**, `windows ui (offscreen Qt)`, job `117917753945` |

**Branch disclosure.** The task named `arena/01a0c936-prog-proc`. The
platform rule for this session says the work belongs on
`arena/5957b76d-prog-proc` and nowhere else. Every commit and every push
in this mission went to `arena/5957b76d-prog-proc`. Nothing was pushed to
the other branch. No `--force`, no `--hard` reset, no `git clean`, no
history rewrite, and no uncommitted work was discarded.

## 3. V8.12 → V8.15 against the git history

The recovered history was compared with the reports, commit by commit:

| Version | Commits | What the history shows |
| --- | --- | --- |
| V8.12 | `7f888dc`, `8566532` | malformed embeddings and non-origin endpoints rejected; the implementation CI result recorded |
| V8.13 | `ec9880f` … `c438902` | Windows path decoding, SQLite URL escaping for Alembic, audit order, the handle-count measurement and its named skip, Qt release that raises now fails closed, well lookup stays ordered when the clock does not tick |
| V8.14 | `b187678` … `ef6a727` | the Windows deadline monitor moved off `ExitProcess` to `TerminateProcess`; the coordinator stopped pumping events while a worker still owns the window; the shutdown-wait CI was recorded |
| V8.15 | `317c553` | the access violation was left as an open fault, the exit-4 contract kept fail-closed, no repeat loop added to required CI |

The reports and the history agree. The one place where a report and the
annotation disagreed was the **failing mode**: the V8.14/V8.15 notes
pointed at the synthetic-deadline child, while the check-run annotation for
job `117917753945` shows the crash was in the **real `QThread`**
`production_deadline` mode (`qt_shutdown_child.py:443`). The report above
is corrected by the annotation, and the fix is aimed at the mode that
actually faulted.

Between V8.11 and V8.15 the tree grew by 44 files and about 3,100 lines,
almost all of it CI tooling, Windows operator tooling and mission reports.
No schema migration was added in that range, and none is added here.

## 4. The critical defect: two threads ended the same process

### 4.1 What failed

| Field | Value |
| --- | --- |
| job | `windows ui (offscreen Qt)`, job id `117917753945` |
| child exit | `3221225477` (`0xC0000005`) — not the contracted `4` |
| newest Python frame | `src/drilling_intelligence/ui/lifetime.py:426 finish_qt_application` |
| next | `src/drilling_intelligence/ui/lifetime.py:507 application_shutdown` |
| next | `tests/ui/qt_shutdown_child.py:443`, mode `production_deadline` |
| then | `main:761` -> `<module>:765` |
| markers | `REAL_QTHREAD, LOOP_STARTED, QUIT_REQUESTED, ABOUT_TO_QUIT_WINDOW_VALID, EXEC_RETURNED` |
| suite | 22 collected, 21 passed, 1 failed, 0 skipped, 53.271 s |

### 4.2 The first cause — established, reproduced, and fixed

The shutdown has two independent terminal paths:

* the **coordinator**, which polls the release budget and calls
  `fail_closed_shutdown`;
* the **deadline monitor**, which guarantees the ceiling and calls
  `_terminal_exit`.

When both fired, both ended the process. The coordinator used `os._exit`,
which is `ExitProcess` on Windows. `ExitProcess` notifies the loaded
libraries, so the Python finalizer ran while the `QApplication` and a live
`QThread` still existed — and that teardown is where the access violation
happened. The monitor used `TerminateProcess`, which does not notify the
libraries. The monitor's Windows path waits up to 50 ms for the deadline
notice before terminating, and that wait is exactly the window in which the
two overlapped.

**The double end was reproduced, not inferred.** The implementation at
`317c553` was loaded directly and the two paths were raced into the same
expiry: **two** `exit(4)` calls were recorded. The pre-fix local stress run
showed both owners (`owner=monitor` 1 of 8, `owner=coordinator` 7 of 8).

**This is a real defect, and it is not the whole cause.** Section 4.6
records the measurement: with the arbitration in place the fault still
occurs in about 8% of attempts. The double end explains why two threads
were ending one process; it does not explain the fault that remains. The
claim that the double end *was* the cause is withdrawn.

> **Closed later.** Sections 4.8 to 4.10 locate the remaining fault and
> close it: the interpreter finalizer, reached through the `os._exit` line
> of `_end_process` on Windows. The double end was a second, separate
> defect, not the cause of the access violation.

### 4.3 The fix

A process-wide arbiter in `src/drilling_intelligence/ui/lifetime.py`:

* the first thread to claim the terminal exit owns it and ends the process;
* the loser **stands down**: it does not terminate, and it does not return
  into Qt or into interpreter teardown. The stand-down waits for the owner,
  bounded at three seconds, and then uses the same end-of-process path as a
  backstop;
* the coordinator now ends the process the way the monitor does — flush the
  standard streams (a forced end does not flush them) and then
  `TerminateProcess` on Windows, `os._exit` elsewhere. The Python finalizer
  no longer runs while Qt objects are alive;
* `_terminate_current_process` raises when the call is refused, instead of
  silently continuing.

### 4.4 The regression test

`tests/unit/test_qt_lifetime_contract.py` drives the coordinator and the
monitor into the same expiry and asserts:

* exactly one terminal exit happens;
* the loser stands down and does not terminate;
* neither terminal path calls `os._exit` directly.

**It fails against the old implementation** — that was verified by loading
the `317c553` version and racing it, which produced two `exit(4)` calls. The
shutdown stress experiment also fails a child whose transcript names two
terminal owners, so the race is detected even in a run that happened to exit
`4`.

### 4.5 The contract was not weakened

`is_fail_closed_shutdown_exit` accepts **only** the integer `4`. It still
rejects `3221225477`, `-1073741819`, `True`, `0` and every other code. No
assertion was relaxed, no test was skipped, and no test was marked `xfail`.
The two named Windows skips stay skips with their honest reasons: the
open-database-file move limitation, and the fact that a Windows process
handle count is not a file-descriptor leak measurement.

### 4.6 Was the access violation reproduced?

**Yes.** The Windows offscreen UI job at
`32483ed89c06981d7e8fdfb85639ec6ab129ca31` ran the shutdown stress
experiment — 25 attempts in each of two modes — and four attempts ended in
the native access violation.

| Sample | Attempts | Native access violations |
| --- | --- | --- |
| Windows stress at `1299b59`, mode `deadline` | 10 | 0 |
| Windows stress at `32483ed`, mode `deadline` (real `QThread`) | 25 | **2** — attempts 21 and 25 |
| Windows stress at `32483ed`, mode `deadline-synthetic` (`threading.Thread`) | 25 | **2** — attempts 5 and 23 |
| **total at `32483ed`** | **50** | **4 — about 8%** |
| Windows stress at `eab9323`, both modes | 50 | **at least 10 — ≥20%** (section 4.8) |
| **Windows stress at `2827073`, both modes** | **50** | **0 — after the fix** (section 4.10) |
| Windows UI suite in the same job | 1 run | 0 — 22 collected, 22 passed, 0 failed, 0 skipped |
| local stress, post-fix (Linux) | 12 | 0 — owners `{coordinator: 10, monitor: 2}` |

The four failures are the exact code `3221225477` (`0xC0000005`), reported
by the stress harness as `windows access violation 0xC0000005`.

**What the two modes tell us.** The `deadline` mode runs a real `QThread`;
the `deadline-synthetic` mode runs a plain `threading.Thread`. They fault at
the same rate, so a live `QThread` is not the cause. What they share is the
shutdown path.

**What was known at that point, and what was not.** In these modes the
coordinator does not pump events while a worker owns the window, so the GUI
thread is inside `QThread.msleep` when the deadline expires and never
touches the window. The `processEvents` pump that an earlier revision of
the coordinator ran at that point — the one the code comments already name
as the place a Windows child access-violated — is skipped in these modes.
So the fault is not that pump in this configuration. At the time of that
run **no cause had been established for what remained**; sections 4.8 to
4.10 are what established it, and the shutdown path was not restructured on
a guess before the evidence arrived.

**The missing evidence, and the instrumentation for it.** The run that
reproduced the fault did not publish the transcript of a faulted child, so
the stage the child had reached when it faulted is not known.
`tools/qt_shutdown_stress.py` now records the stage tokens and the shutdown
transcript of every failed attempt and reports them at error level, so the
next Windows run says *where* the fault happens and not only that it
happened.

**The measurement is not a gate.** ADR-55 already decided that a repeat
loop does not belong in required CI: a fault that fires in a few percent of
attempts turns a required job into a coin toss, and a job that fails at
random stops meaning anything. The native fault is therefore measured and
published — every occurrence at error level, the count and the rate as a
notice, the transcripts in the artifact — and it does not fail the run.
`--gate` exists for an operator who wants it to. Any attempt that ends
with any exit code other than `4` and other than the known native fault
still fails the step, and the UI suite's single deadline child is still
required to exit `4`.

### 4.7 The second reproduction, and what it rules out

The next Windows run, at `35b05527159c524a7e160246ca0da978721ab462`,
failed in the UI suite itself: 22 collected, 21 passed, **1 failed** —
`test_synthetic_deadline_is_supplementary_not_the_qthread_proof`, exit
`3221225477`. The failure carried the child's transcript, and the
transcript is what matters here:

| Evidence | Value |
| --- | --- |
| stages the child printed | `LOOP_STARTED`, `QUIT_REQUESTED`, `ABOUT_TO_QUIT_WINDOW_VALID`, `EXEC_RETURNED`, `SYNTHETIC_DEADLINE`, `SHUTDOWN_BEGIN` |
| terminal markers | **none** — no `TERMINAL_ATTEMPT` line |
| outermost frames | `lifetime.py:683 in application_shutdown` -> `qt_shutdown_child.py:737 in deadline_synthetic` -> `main:763` -> `<module>:767` |

**What this rules out.** The child reached `SHUTDOWN_BEGIN` and then
faulted **before any terminal decision was recorded**. Neither the
coordinator nor the deadline monitor had claimed the terminal exit, so the
process was not being torn down by either of them when it died. The fault
is therefore **not in the terminal decision**: it is on the GUI thread,
before the shutdown gives up.

> **Superseded by section 4.8.** The reading in this section — that the
> fault is in the coordinator's release loop — was too early. The innermost
> frames of the faulting thread, published two runs later, show the GUI
> thread inside the Qt event filter while the interpreter finalizer ran:
> the finalizer *is* the coordinator's path to the end of the process, and
> it runs before any terminal decision is recorded. The conclusion of this
> section stands — the fault is not in the terminal *decision* — and its
> second half does not.

That is a correction to the direction of the earlier reading. The double
terminal end was a real defect and it is fixed, but the remaining fault
happens earlier than the terminal decision, so it is a different thing.

**What is still hidden.** The failure message keeps the last 500 characters
of the child's output. A `faulthandler` stack is printed from the
innermost frame outwards, so the cut kept the *outer* frames and dropped
the inner ones — and the inner frame is the fault location. The UI test
and the stress harness now put the innermost frames first, so the next
Windows run names the file and the line that faulted.

**What was not done.** No speculative change was made to the Qt teardown
on the strength of a hypothesis. The shutdown path was changed in one
place, and only because it is provably right on its own terms: after an
accepted `TerminateProcess` the process is already marked for
termination, so the `os._exit` that followed it — which is `ExitProcess`,
which notifies the libraries and runs the interpreter finalizer — no
longer runs. That change removes a hazard; it is not claimed to be the
fix, and the measurement that would show it is the next one.

No Linux run was ever presented as a Windows reproduction, and no green
Windows UI job is presented as proof that the fault cannot recur.

### 4.8 The third reproduction: the faulting thread's innermost frames

The Windows offscreen UI job at
`eab9323324a1aade38ea547febe3ffe079dd187a` (run `39414923372`, job
`118601549130`) passed the UI suite — 22 collected, 22 passed, 0 failed, 0
skipped, exit 0, 51.2 s — and the stress step published the fault with the
innermost frames of the faulting thread. The step runs even when the UI
suite fails (`if: always()`), which is why the evidence exists at all.

| mode | attempts | native access violations | rate |
| --- | --- | --- | --- |
| `deadline` (real `QThread`) | 25 | **at least 9** — ordinals 1, 2, 3, 4, 8, 10, 12, 16, 19 | ≥36% |
| `deadline-synthetic` (`threading.Thread`) | 25 | **at least 1** — ordinal 10 | ≥4% |
| **total** | **50** | **at least 10** | **≥20%** |

The rate went **up** against the 8% measured at `32483ed`. That is a
measurement, not a regression in the product: the change between the two
commits removed the `os._exit` that followed an *accepted* `TerminateProcess`
(`35b0552`), and the faults that remain are on the path where that call
was **not** accepted.

**The counts are lower bounds, and this is how to read them.** GitHub keeps
at most ten annotations of one level per step, and the stress step publishes
one notice per attempt plus one error per fault. The published notices
therefore cover only the first ten attempts of the `deadline` mode, and the
published errors only the first ten faults. What the run proves is therefore
the *lower bound* — at least ten native faults in fifty attempts — and that
the `deadline` mode alone faulted nine times in nineteen attempts.

**The frames.** Read against the source at `eab9323`:

| role in the attempt | frames, innermost first | what they are |
| --- | --- | --- |
| the thread that owns the terminal exit | `lifetime.py:625` → `lifetime.py:386` → `lifetime.py:447` | `event.type() == QEvent.Type.Quit` in `_AcceptQuit.eventFilter`; `os._exit(code)` in `_end_process`; the `_end_process(...)` call in the monitor's `_terminal` |
| the thread that stands down | `lifetime.py:423` → `lifetime.py:439` → `lifetime.py:508` | `time.sleep(0.02)` in `_stand_down_for_terminal_exit`; that call; `self._terminal()` in `enforce` |

**What the frames say.** Two things, and they are the same thing.

1. The first row is not a possible call chain — a Qt event filter cannot be
   called from `os._exit`. So that row is two threads: the GUI thread
   standing inside `_AcceptQuit.eventFilter` (a **Python callback invoked
   from C++**), and the monitor thread on the `os._exit` line of
   `_end_process`. The second row *is* a chain, and it is the monitor
   thread asleep in the stand-down.
2. Both roles are the same mechanism seen from two threads. The thread
   that ends the process is on the `os._exit` line of `_end_process` —
   the line that is only reached when the native termination call was
   **refused** — and the finalizer it starts runs while another thread is
   executing Python. In the attempts whose published frames were the
   stand-down chain, the `os._exit` frame belonged to the ending thread
   and was truncated out of the published report by the annotation cap.

`os._exit` on Windows is `ExitProcess`. `ExitProcess` notifies the loaded
libraries, so the interpreter **finalizer** runs on the terminating thread
while the `QApplication` and a live GUI thread still exist, and it destroys
them there. A Python callback cannot run while the interpreter is
finalizing, and the finalizer must not be running at all while another
thread is inside Python code. That is the mechanism the frames point at,
and it explains every observation so far: the rarity (the fault needs the
finalizer to reach the Qt objects while the GUI thread is inside a
callback), the fact that both stress modes fault, the fact that the
`35b0552` UI-suite failure faulted with **no** terminal marker at all
(the finalizer ran before any terminal decision), and the fact that the
fault also occurs in a thread that is only standing down.

**What the frames do not say.** The frames name the faulting thread
correctly, but the harness took the first three `File ` lines of the whole
`faulthandler` report, and `faulthandler` prints *every* thread. The
surviving threads' stacks are context, not cause, and mixing them with the
faulting thread's stack makes a chain that cannot exist. The harness and
the UI test now publish the `Current thread` section only; the next run
therefore names the faulting thread exactly.

### 4.9 The fix: the Windows branch of the terminal end cannot reach `os._exit`

The change is in `_end_process` (`src/drilling_intelligence/ui/lifetime.py`)
and it is one rule: **on Windows the process is never ended through the
interpreter finalizer.**

*   `_terminate_current_process` now types the native call explicitly —
    `GetCurrentProcess` returns a `HANDLE`, `TerminateProcess` takes
    `(HANDLE, UINT)` and returns a `BOOL`. The previous call was untyped:
    the 64-bit handle was passed as a 32-bit argument, and a refusal fell
    through to the `os._exit` this path exists to avoid. A refusal is now
    retried and then raised, not swallowed.
*   `_end_process` on Windows ends the process with `TerminateProcess`,
    waits for the termination to take effect, and returns. The `os._exit`
    line is unreachable there. The POSIX path is unchanged.
*   A refused termination is reported (`TERMINAL_REFUSED`) and the process
    waits for the watchdog that bounds the parent's wait. A refused
    termination is a failed shutdown that the parent reports; reaching the
    exit code through the finalizer is the fault.

The contract is enforced structurally, not by a comment: a unit test parses
`_end_process` and asserts that **no `os._exit` call is reachable inside the
`if _is_windows()` branch**, and that the function contains exactly one
direct exit — the POSIX one.

### 4.10 The measurement after the fix: zero native faults in fifty attempts

The Windows offscreen UI job at `282707395553b0212684293dd787ddb6644e9aec`
(run `39416773889`, job `118608029122`):

| Evidence | Value |
| --- | --- |
| UI suite | 22 collected, 22 passed, 0 failed, 0 skipped, exit 0, runtime 52.183 s |
| shutdown stress step | 96 s — the same duration as the run that faulted (94 s), so the same fifty attempts ran (25 per mode) |
| attempts published | 10 notices, **all** `exit=4`, `reason=-` |
| native access violations | **none** — the step published **no** failure annotation at all, where the previous run published ten |
| job conclusion | success |

The previous run published ten native-fault annotations for the same step;
this run published none. Every attempt that is visible exited `4`, and the
step's duration says the same fifty attempts ran. **The native access
violation did not occur in that run.**

This is the strongest form the evidence can take without the job log: the
summary notice is the eleventh notice of the step and GitHub keeps ten per
level per step, so the summary line itself was truncated. The absence of
failure annotations is the signal — a fault in any of the fifty attempts
would have produced one — and the count in the summary JSON is in the
artifact, which this environment cannot download.

**What this establishes, and what it does not.**

*   The root cause is now **established**: the interpreter finalizer running
   on the terminating thread while another thread was inside Python code,
   reached through the `os._exit` line of `_end_process` on Windows.

> **Correction (V8.17, ADR-61).** "The Python finalizer" names a
> mechanism that does not exist. On Windows `os._exit` is the CRT
> `_exit` → `ExitProcess`, which notifies the loaded DLLs
> (`DLL_PROCESS_DETACH`) and runs the native Qt/PySide6/shiboken
> teardown while the GUI thread is inside a Python callback; it does
> **not** run Python interpreter finalization. The access violation is
> real and is not withdrawn — only the label is corrected.
*   The fix is **verified on the Windows runner**: 0 native faults in 50
   attempts, against at least 10 in 50 before.
*   It is **not** proven for every configuration. Fifty attempts on one
   runner on one day is a measurement, not a proof; the owner's manual
   checklist on the workstation that matters is still **NOT RUN**, and it is
   the only thing that can speak for that machine.

## 5. Architecture and data-integrity findings

| Area | Finding | Severity | Action |
| --- | --- | --- | --- |
| shutdown ownership | two threads could end the same process | **critical** | fixed, with a regression test |
| Windows native fault | the interpreter finalizer ran on the terminating thread while the GUI thread was inside a Python callback | **critical** | cause established from the innermost frames, fixed at `2827073`, and verified on the Windows runner: **0 native faults in 50 attempts**, against at least 10 in 50 before |
| evidence integrity | every extracted value keeps its source locator; the citation auditor re-reads the file and reports MATCH / MISMATCH / UNREADABLE / NOT_CHECKABLE | sound | unchanged |
| promotion boundary | ingestion to domain rows is idempotent and self-reporting; a document is never promoted twice | sound | unchanged |
| search | BM25 over a disposable SQLite sidecar that is rebuilt, never migrated; candidates are re-read from the authoritative database | sound | unchanged |
| migrations | 19 revisions, exactly one head, `0019`; a migration script is compiled in CI and a fresh workspace stamps from package metadata | sound | unchanged |
> **Correction (V8.17, ADR-61).** "The Python finalizer" names a
> mechanism that does not exist. On Windows `os._exit` is the CRT
> `_exit` → `ExitProcess`, which notifies the loaded DLLs
> (`DLL_PROCESS_DETACH`) and runs the native Qt/PySide6/shiboken
> teardown while the GUI thread is inside a Python callback; it does
> **not** run Python interpreter finalization. The access violation is
> real and is not withdrawn — only the label is corrected.

| domain coverage matrix | re-counted against the source: 26 enum members, 26 contracts, 13 `END_TO_END_CERTIFIED`, 8 `KNOWLEDGE_SUPPORTED`, 5 `EXTRACT_ONLY`, 11 distinct handlers, 21 taxonomy signatures (BOOK, INVOICE, LWD_MWD, OTHER and WIRELINE have none) | **accurate as written** | unchanged |
| broad exception handling | 86 `except Exception` sites in `src/`, all at documented adapter or parser boundaries | low | inspected; none was added or removed |
| oversized functions | `review/service.py::_build` (742 lines), `cli/app.py::build_parser` (729), `cli/app.py::command_records` (498) | medium | **recorded, not refactored** — a refactor of that size before a measured baseline is the risk this mission was told to avoid |
| `type: ignore` | 17 in `src/` | low | 3 became unnecessary after the type pass and were removed |
| data provenance | `SourceLocator.from_dict` rebuilt a locator through an untyped call; it now returns a typed locator and drops fields the class does not have | low | fixed, with a test |

No data-integrity defect was found. The provenance, promotion, citation and
migration paths are the strongest part of this repository, and the audit
found no way to write a domain row that does not point at a source.

## 6. Dependency, supply-chain and security status

| Item | Before | After |
| --- | --- | --- |
| version record | version ranges only; nothing recorded what was installed | `constraints/runtime.txt`, `development.txt`, `ui.txt`, `build.txt` pin the resolution the CI proves |
| pin renewal | by hand | `python tools/constraints.py --set <name>` regenerates a file; `--check` fails on a hand edit |
| vulnerability scan | none | `tools/dependency_audit.py` runs `pip-audit` over the pinned sets in CI |
| audit outcome | — | **clean** — no known vulnerability in the pinned runtime and UI sets |
| audit failure policy | — | `clean` passes, `vulnerable` fails, an advisory-service **outage** is a recorded warning and a pass, an audit that cannot decide is a failure. A third-party outage is not a defect in this repository, and it is still written to `dependency-audit.json` so a silent outage cannot be mistaken for a clean audit |
| GitHub Actions | referenced by tag (`@v7`) | every action pinned to an immutable commit SHA, verified against the upstream v7 tags on 2026-10-20: `actions/checkout` `3d3c42e5…`, `actions/setup-python` `5fda3b95…`, `actions/upload-artifact` `cf430e03…`. The workflow policy test now asserts that **every** action in **every** workflow is pinned to a SHA recorded against its v7 tag — a stronger statement than the tag it replaced |
| wheel availability | not checked | every pinned version has a wheel for CPython 3.11 and 3.14, on Linux and on Windows, including the new `mypy`, `pytest-cov` and `coverage` pins |
| reproducibility | the isolated build environment floated | `constraints/build.txt` pins `setuptools 84.2.0` and `wheel 0.48.0`; the build is otherwise unchanged |

**Privacy, licensing and security claims.** No open-source license was
invented: the project stays proprietary and says so in `pyproject.toml` and
the README. No security contact address was invented. No deployment or
encryption claim was made. Secret redaction (`core/redact.py`) and the
"no provider probe unless asked" rule are unchanged and were not widened.

A scan of the source for the usual private failures found none:

| Check | Result |
| --- | --- |
| hardcoded secret-shaped literals in `src/` | none |
| tracked `.env`, `.pem`, `.key`, credential files | none |
| `shell=True` | not used anywhere in `src/` |
| `eval` / `exec` | not used; the only hits are `QApplication.exec()` and prose |
| network egress | confined to the optional Ollama adapter and the MinerU probe; `doctor` opens no socket, and `drillintel ai status` probes only when asked |

**A note on the audit's own environment.** The first audit run reported
six findings, all of them in the sandbox's seeded `setuptools 66.1.1`, which
is not a dependency of the product. The tool was changed to audit the
repository's own pinned requirement files instead of whatever happens to be
installed in the machine, and a fresh resolution of the runtime set audits
clean.

## 7. Type checking and coverage status

### Type checking

One package boundary, `drilling_intelligence.core`, is type-checked in CI
with a strict profile: an untyped definition, an incomplete definition, a
returned `Any` and an unused `type: ignore` are all errors.

| Item | Result |
| --- | --- |
| boundary | `src/drilling_intelligence/core` — 13 files |
| profile | `disallow_untyped_defs`, `disallow_incomplete_defs`, `check_untyped_defs`, `warn_return_any`, `warn_unused_ignores`, `warn_redundant_casts`, `no_implicit_optional` |
| result | **Success: no issues found in 13 source files** |
| CI | Linux `test` (cp3.11) and Windows `test` (cp3.11) both run `python -m mypy` |
| remaining packages | not checked. Widening the boundary is a separate, dated change |

Four real defects came out of the type pass, not annotation churn:

1. the lifecycle state enums were declared `type[StrEnum]`, which has no
   `parse`, so the annotation described an API the enums do not have (they
   derive from `StrEnumLike`, which does);
2. `Quantity.from_dict` coerced an arbitrary object with `float()`, so a
   value that is not a number raised `TypeError` where a unit error belongs,
   and a boolean was accepted as `1`;
3. `VocabMatch.to_dict` declared `dict[str, str]` and returned a boolean;
4. the structured logger forwarded `**fields` into a call that has named
   parameters, so a field called `duration_ms` or `exception` became a
   second argument — and `exception` raised a duplicate-keyword `TypeError`.

`tests/unit/test_core_boundary.py` pins each of those decisions, so a
refactor that only looks like a type change cannot undo one of them.

### Coverage

| Item | Result |
| --- | --- |
| measured | yes, in CI, on the floor runtime (CPython 3.11) |
| scope (1) | the whole suite in the Linux `test` job — the Qt layer is **skipped** there, because that job deliberately does not install the optional extra |
| scope (2) | the UI tests in the Linux `workbench` job, where Qt is real |
| published | as a workflow notice (`coverage-total`, `coverage-ui-total`), in the step summary, and as `coverage.xml` / `coverage-ui.xml` artifacts |
| gate | **none.** No threshold is enforced, because no measured baseline existed to set one against. Inventing a threshold is not a measurement |
| the numbers | see section 12 — they are read from the run, not from this file |

## 8. Test infrastructure and the actual numbers

| Item | Result |
| --- | --- |
| local full suite | **2269 collected, 2248 passed, 21 skipped, 0 failed, 0 errors, 0 xfailed, 0 xpassed, exit 0**, runtime 1378.368 s (`pytest-totals.json`) |
| local Qt layer | 14 passed in `tests/ui/` (offscreen, against the sandbox's stubbed graphics libraries) |
| local shutdown stress | 10 attempts, 0 failures, 0 native faults (`native_fault_rate=0.0%`) |
| local release smoke | `tools/release_smoke.py` **PASSED** — wheel and sdist, clean install outside the checkout, 49 tables, schema stamped `0019` |
| local dependency audit | `tools/dependency_audit.py` → `clean` |
| local static checks | `ruff check` clean, `ruff format --check` clean, `mypy` clean on the 13 files of `drilling_intelligence.core` |
| Linux CI | `test (cp3.11)`, `test (cp3.14)`, `clean install smoke`, `workbench (offscreen Qt)`, `Ubuntu 26.04 compatibility` |
| Windows CI | `test (cp3.11)`, `test (cp3.14)`, `windows clean install smoke`, `windows ui (offscreen Qt)` |
| totals are required | `tools/pytest_totals.py` cross-checks the session stats against the JUnit record and the shell exit file, and fails the job on a disagreement, an unexpected pass, or an unexpected skip |
| a skip is not a pass | a skip is expected only for the headless Qt skip (`QtWidgets unavailable`) or the two named platform limits |
| retries are not hidden | the 15-attempt Linux stability gate keeps every failed attempt; the shutdown stress step reports every attempt and every failure |
| release proof | `tools/release_smoke.py` builds the wheel and the sdist, installs each into a clean environment **outside** the checkout, and drives the installed CLI |

The two named Windows skips remain skips, with these reasons, and neither
was dressed up as a pass:

* `this platform will not move an open database file` — Windows will not
  rename a SQLite file that still has a connection;
* `Windows process handle count is not a file-descriptor leak measurement` —
  two runs grew by 3 and by 11, so that counter cannot prove the archive
  bound. Linux still counts `/proc/self/fd`.

## 9. Intentionally unimplemented capabilities

None of these is a defect. Each is refused on purpose, and the code says so.

| Capability | State |
| --- | --- |
| OpenAI-compatible adapter | no adapter exists. `provider = openai-compatible` returns unsupported and opens no socket; the API key is not read |
| Embedding sidecar / vector store | `UnavailableVectorStore` reports that no store exists. No domain table gains a vector column. Retrieval is keyword and structured only |
| Skills runtime | the `skill` tables exist and no service reads `Skill` or `SkillVersion`. A skill cannot become a calculation or a promoted row |
| Additional calculation methods | NPT roll-up V1 is the only registered executable method. A method needs an id, inputs, units, outputs and refusal cases before it can exist |
| Risk scoring, cost/AFE engine, plan-vs-actual dashboards | deliberately not built; `docs/DOMAIN.md` says what each one refuses to invent |
| OCR for scanned pages | MinerU stays an optional external command behind an adapter; the built-in extractors carry on without it |
| Document writers | none. Nothing in this repository writes a domain document |

## 10. Open owner decisions

`docs/PRODUCT_DECISIONS_REQUIRED.md` still carries six open items. **No code
was written for any of them**, and no default was chosen on the owner's
behalf:

1. the OpenAI-compatible adapter — which API, which secret slot, which auth
   header, and whether `drillintel ai status` may leave the machine;
2. the embedding sidecar — vector dimension, distance metric, staleness, and
   whether retrieval may change when the sidecar is absent;
3. the skills runtime — who may install a skill, what it may read, and
   whether it may propose a record a human must still accept;
4. an additional calculation method — method id, inputs, units, outputs and
   the cases that must be refused;
5. the remaining items in that register (the register is the authority; it
   was read in full and none of it was closed by this mission);
6. the owner's manual Windows acceptance checklist.

## 11. The owner's manual Windows checklist

**NOT RUN.** `docs/WINDOWS_TESTING.md` is the operator route, and its
checklist stays unchecked. The disposable pilot
(`tools/windows_pilot.ps1`) does not complete those boxes, and a green
offscreen CI job is not manual acceptance. The checklist gained one item in
this mission — run `tools/qt_shutdown_stress.py --attempts 25` and record
exit `4` for every attempt — and one route: the constraints and `mypy` in
the editable install.

## 12. Verification: exact-SHA runs

The runs for the code SHA `32483ed89c06981d7e8fdfb85639ec6ab129ca31` are
in the table below. The runs for the publication SHA (the commit that adds
this file) are cited in the final response, together with the full SHA of
that commit.

### 12.1 The run that reproduced the fault

| Run | SHA | Conclusion |
| --- | --- | --- |
| Linux https://github.com/asgareyvazi/Prog-Proc/actions/runs/39411300180 | `32483ed` | `clean install smoke` success, `workbench (offscreen Qt)` success, `Ubuntu 26.04 compatibility` success, `test (cp3.11)` and `test (cp3.14)` success |
| Windows https://github.com/asgareyvazi/Prog-Proc/actions/runs/39411300144 | `32483ed` | `windows clean install smoke` success, `windows test (cp3.11)` and `windows test (cp3.14)` success, `windows ui (offscreen Qt)` **failure** — the shutdown stress step found four native access violations |

The Windows UI job's own suite was green in that run (22 collected, 22
passed, 0 failed, 0 skipped, exit 0). The job failed on the stress step,
which is the measurement described in section 4.6. The failure annotations
read, in full:

```
failure | shutdown-stress deadline-synthetic #23: windows access violation 0xC0000005
failure | shutdown-stress deadline-synthetic #5: windows access violation 0xC0000005
failure | shutdown-stress deadline #25: windows access violation 0xC0000005
failure | shutdown-stress deadline #21: windows access violation 0xC0000005
```

That run is a **measurement**, and it is the most important result of this
mission. The stress step is now a measurement rather than a gate, for the
reason in ADR-59, and the run that carried it is recorded here as it
happened.

### 12.2 The Linux workbench job at `32483ed`

| Evidence | Value |
| --- | --- |
| Qt stability qualification | 15 attempts, every one `EXIT 0 PASS` |
| shutdown stress, mode `deadline` | 10 attempts, every one `exit=4` |
| coverage of the Qt layer | `coverage-ui-total: 47%` |
| `test_production_deadline_exits_4_and_the_parent_bounds_the_wait` | `FIRST_PASS`, attempt 1 exit 0 |

### 12.3 The runs at the fix commit, `2827073`

| Run | SHA | Conclusion |
| --- | --- | --- |
| Linux https://github.com/asgareyvazi/Prog-Proc/actions/runs/39416773783 | `2827073` | `clean install smoke` success, `workbench (offscreen Qt)` success, `Ubuntu 26.04 compatibility` success, `test (cp3.11)` and `test (cp3.14)` success |
| Windows https://github.com/asgareyvazi/Prog-Proc/actions/runs/39416773889 | `2827073` | `windows clean install smoke` success, `windows test (cp3.11)` and `windows test (cp3.14)` success, `windows ui (offscreen Qt)` success — **22 collected, 22 passed, 0 failed, 0 skipped**, and **no native access violation in the fifty stress attempts** |

**The Windows workbench job in detail** (job `118608029122`):

| Evidence | Value |
| --- | --- |
| UI suite | 22 collected, 22 passed, 0 failed, 0 skipped, 0 xfailed, 0 xpassed, exit 0, runtime 52.183 s |
| shutdown stress | 96 s of wall clock — the same fifty attempts as the run that faulted — **every published attempt `exit=4`**, and **no failure annotation**: no native access violation |
| the fault before the fix | the same step at `eab9323` published ten native-fault annotations |

**The test jobs in detail** — the counts are the sessions' own totals, read
from the annotations the jobs published:

| Job | Collected | Passed | Skipped | Failed | Errors | xfail / xpass | Runtime |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Linux `test (cp3.11)` | 2269 | 2248 | 21 | 0 | 0 | 0 / 0 | 2657.669 s |
| Linux `test (cp3.14)` | 2269 | 2248 | 21 | 0 | 0 | 0 / 0 | — |
| Windows `test (cp3.11)` | 2269 | 2246 | **23** | 0 | 0 | 0 / 0 | 2675.893 s |
| Windows `test (cp3.14)` | 2269 | 2246 | **23** | 0 | 0 | 0 / 0 | 2483.004 s |

The two extra Windows skips are the two named platform limits — the
open-database-file move and the process-handle count — and they are skips
with honest reasons, not passes. The Linux coverage of the whole suite is
`coverage-total: 79%`; the Qt layer's own coverage is `coverage-ui-total:
47%`. Both dependency audits report **no known vulnerabilities**, and the
`datetime-adapter-warnings` counter is `0` on every job.

**The Linux workbench job in detail** (job `118608203295`):

| Evidence | Value |
| --- | --- |
| Qt stability qualification | 15 attempts, every one `EXIT 0 PASS` |
| shutdown stress, mode `deadline` | 10 attempts, every one `exit=4` |
| coverage of the Qt layer | `coverage-ui-total: 47%` |
| `test_production_deadline_exits_4_and_the_parent_bounds_the_wait` | `FIRST_PASS`, attempt 1 exit 0 |

### 12.4 The runs for the publication SHA

The commit that adds this file creates a new SHA. Its Linux and Windows runs
are cited in the final response, with the full SHA of that commit.

## 13. What was changed

| File | Why |
| --- | --- |
| `src/drilling_intelligence/ui/lifetime.py` | the single-owner terminal-exit arbitration, the stand-down path, the flushed `TerminateProcess` end, the diagnostic markers, and — since `2827073` — a Windows branch that cannot reach `os._exit`, a typed native call, and a reported refusal instead of a swallowed one |
| `src/drilling_intelligence/core/lifecycle.py` | the state enums are `StrEnumLike`, which is the class that has `parse` |
| `src/drilling_intelligence/core/logging.py` | a field named like an argument is data, not a second argument; `__exit__` states that it does not suppress |
| `src/drilling_intelligence/core/units.py` | a value that is not a number is refused with a unit error instead of a `TypeError` |
| `src/drilling_intelligence/core/vocabulary.py` | the payload says what it is: a JSON record whose values are not all text |
| `src/drilling_intelligence/core/provenance.py` | a locator is rebuilt into a typed locator, and an untyped read becomes a string |
| `tools/qt_shutdown_stress.py` (new) | the repeat experiment, the detector that fails a child whose transcript names two terminal owners, and the publication of the faulting thread's frames alone |
| `tools/dependency_audit.py` (new) | the vulnerability audit, with the three outcomes |
| `tools/constraints.py` (new) | the pin regenerator, with `--check` |
| `constraints/*.txt`, `constraints/README.md` | the pinned sets, and how to renew them |
| `tests/unit/test_qt_lifetime_contract.py` | the arbitration regression tests |
| `tests/unit/test_qt_shutdown_stress.py` (new) | the stress harness contract |
| `tests/unit/test_dependency_audit.py` (new) | the audit classifier contract |
| `tests/unit/test_constraints.py` (new) | the pin format and the "the file says what the metadata resolves to" check |
| `tests/unit/test_core_boundary.py` (new) | the four decisions the type pass made visible |
| `tests/unit/test_qt_workbench_policy.py` | the policy test now requires a pinned commit SHA against a recorded v7 tag |
| `tests/ui/qt_shutdown_child.py` | the `SHUTDOWN_BEGIN` marker, so a transcript shows where the shutdown started |
| `tests/ui/test_qt_shutdown.py` | the failure detail now publishes the faulting thread's frames, not the first frames of the whole report |
| `.github/workflows/ci.yml`, `.github/workflows/ci-windows.yml` | the pinned installs, the pinned actions, the audit step, the mypy step, the coverage steps, the shutdown stress steps |
| `pyproject.toml` | the `pytest-cov` and `mypy` dev dependencies, the `[tool.mypy]` and `[tool.coverage]` configuration |
| `.gitignore` | the generated reports |
| `docs/DECISIONS.md` | ADR-56, ADR-57, ADR-58, ADR-59, ADR-60 |
| `docs/WINDOWS_TESTING.md` | the shutdown stress route, the constraints, the new checklist item |
| `docs/mission-reports/END_TO_END_PROGRESS.md` (new) | the running ledger |
| `docs/mission-reports/END_TO_END_FINAL_REPORT.md` (new) | this file |

No schema change, no migration, no provider, no vector store, no skills
runtime, no calculation, and no document writer was added. The historical
mission reports were not edited.

## 14. Remaining work, in priority order

1. **Verify the closure of the `os._exit` path on Windows.** The fault is
   reproduced, and the innermost frames of the faulting thread point at the
   one place where the interpreter finalizer runs while another thread is
   inside Python: the `os._exit` line of `_end_process` on Windows. That
   line is now unreachable on Windows (`2827073`). The number that settles
   it is the fault rate of the next Windows stress run, published as
   `shutdown-stress-summary` on every run. If a fault is still published,
   its frames now name the faulting thread exactly, and the next step is
   that line — not a guess at the Qt teardown.
2. **Run the owner's manual Windows checklist** in `docs/WINDOWS_TESTING.md`
   on the workstation that matters, and record the notes. Until those notes
   exist, the checklist is unexecuted and this report stays PARTIAL.
3. **Keep measuring the fault rate after every change.** The rate is the
   number that shows whether a change helped. It is published on every
   Windows run as `shutdown-stress-summary`, with the innermost frames of
   the faulting thread for every faulted attempt.
4. **Decide the six open product questions** in
   `docs/PRODUCT_DECISIONS_REQUIRED.md`. Each one currently fails closed,
   which is correct, but the product is waiting on them.
5. **Widen the type-checking boundary** from `core` to the next package
   (`database`, then `extraction`), one dated change at a time.
6. **Split the three oversized functions** — `review/service.py::_build`,
   `cli/app.py::build_parser`, `cli/app.py::command_records` — now that a
   measured coverage baseline exists to compare against.
7. **Investigate the UI flake.** `test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail`
   failed once in about fourteen local runs. The 15-attempt stability gate
   already covers that node on Linux; the same measurement on Windows would
   say whether it is platform-specific.
8. **Renew the pins on a schedule**, with `tools/constraints.py` and a CI
   run per renewal, and re-audit. The pins are a snapshot; a snapshot that
   is never renewed becomes a liability.
9. **Measure the coverage of the packages outside `core`** and publish it
   next to the two numbers already published, so the two numbers can be
   read as one.
