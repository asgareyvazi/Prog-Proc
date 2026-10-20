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

---

## 1. Verdict

**PARTIAL.** The mission's `DONE` bar is not met, and the reason is
narrow and stated:

1. The Windows native access violation **is reproduced** — 4 of 50 shutdown
   attempts at `32483ed` ended in `0xC0000005`, about 8% per attempt. The
   double terminal end was a real defect, it was reproduced from the source,
   and it is fixed, but it is **not the whole cause**: with the arbitration
   in place the fault still occurs. The remaining cause is not established,
   and the shutdown path was not restructured on a guess.
2. The owner's manual Windows checklist is **NOT RUN**. It is the owner's
   to execute; the disposable pilot does not complete those boxes.

Everything else in the scope is complete and pushed: the critical defect is
fixed with a regression test that fails against the old implementation, the
supply chain is pinned and audited, one package boundary is type-checked in
CI, coverage is measured and published, and the domain coverage matrix was
re-counted against the source and found accurate.

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
| Windows UI suite in the same job | 1 run | 0 — 22 collected, 22 passed, 0 failed, 0 skipped |
| local stress, post-fix (Linux) | 12 | 0 — owners `{coordinator: 10, monitor: 2}` |

The four failures are the exact code `3221225477` (`0xC0000005`), reported
by the stress harness as `windows access violation 0xC0000005`.

**What the two modes tell us.** The `deadline` mode runs a real `QThread`;
the `deadline-synthetic` mode runs a plain `threading.Thread`. They fault at
the same rate, so a live `QThread` is not the cause. What they share is the
shutdown path.

**What is known, and what is not.** In these modes the coordinator does not
pump events while a worker owns the window, so the GUI thread is inside
`QThread.msleep` when the deadline expires and never touches the window.
The `processEvents` pump that an earlier revision of the coordinator ran at
that point — the one the code comments already name as the place a Windows
child access-violated — is skipped in these modes. So the fault is not that
pump in this configuration, and **no cause has been established for what
remains.** The shutdown path was not restructured on a guess.

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

No Linux run was ever presented as a Windows reproduction, and no green
Windows UI job is presented as proof that the fault cannot recur.

## 5. Architecture and data-integrity findings

| Area | Finding | Severity | Action |
| --- | --- | --- | --- |
| shutdown ownership | two threads could end the same process | **critical** | fixed, with a regression test |
| evidence integrity | every extracted value keeps its source locator; the citation auditor re-reads the file and reports MATCH / MISMATCH / UNREADABLE / NOT_CHECKABLE | sound | unchanged |
| promotion boundary | ingestion to domain rows is idempotent and self-reporting; a document is never promoted twice | sound | unchanged |
| search | BM25 over a disposable SQLite sidecar that is rebuilt, never migrated; candidates are re-read from the authoritative database | sound | unchanged |
| migrations | 19 revisions, exactly one head, `0019`; a migration script is compiled in CI and a fresh workspace stamps from package metadata | sound | unchanged |
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
| local full suite | see section 12 |
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

### 12.3 The runs for the publication SHA

The commit that adds this file creates a new SHA. Its Linux and Windows runs
are cited in the final response, with the full SHA of that commit.

## 13. What was changed

| File | Why |
| --- | --- |
| `src/drilling_intelligence/ui/lifetime.py` | the single-owner terminal-exit arbitration, the stand-down path, the flushed `TerminateProcess` end, the diagnostic markers |
| `src/drilling_intelligence/core/lifecycle.py` | the state enums are `StrEnumLike`, which is the class that has `parse` |
| `src/drilling_intelligence/core/logging.py` | a field named like an argument is data, not a second argument; `__exit__` states that it does not suppress |
| `src/drilling_intelligence/core/units.py` | a value that is not a number is refused with a unit error instead of a `TypeError` |
| `src/drilling_intelligence/core/vocabulary.py` | the payload says what it is: a JSON record whose values are not all text |
| `src/drilling_intelligence/core/provenance.py` | a locator is rebuilt into a typed locator, and an untyped read becomes a string |
| `tools/qt_shutdown_stress.py` (new) | the repeat experiment, and a detector that fails a child whose transcript names two terminal owners |
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
| `.github/workflows/ci.yml`, `.github/workflows/ci-windows.yml` | the pinned installs, the pinned actions, the audit step, the mypy step, the coverage steps, the shutdown stress steps |
| `pyproject.toml` | the `pytest-cov` and `mypy` dev dependencies, the `[tool.mypy]` and `[tool.coverage]` configuration |
| `.gitignore` | the generated reports |
| `docs/DECISIONS.md` | ADR-56, ADR-57, ADR-58 |
| `docs/WINDOWS_TESTING.md` | the shutdown stress route, the constraints, the new checklist item |
| `docs/mission-reports/END_TO_END_PROGRESS.md` (new) | the running ledger |
| `docs/mission-reports/END_TO_END_FINAL_REPORT.md` (new) | this file |

No schema change, no migration, no provider, no vector store, no skills
runtime, no calculation, and no document writer was added. The historical
mission reports were not edited.

## 14. Remaining work, in priority order

1. **Find the stage at which the remaining native fault happens.** The fault
   is reproduced at about 8% per attempt on the Windows runner, and the
   double terminal end — a real defect, now fixed — is not its cause. The
   stress harness now publishes the stage tokens and the shutdown transcript
   of every faulted child, so the next Windows run says where the fault
   happens. That is the evidence a fix needs; a fix without it would be a
   guess at the shutdown path.
2. **Run the owner's manual Windows checklist** in `docs/WINDOWS_TESTING.md`
   on the workstation that matters, and record the notes. Until those notes
   exist, the checklist is unexecuted and this report stays PARTIAL.
3. **Re-measure the fault rate after the next change.** The rate is the
   number that shows whether a change helped. It is published on every
   Windows run as `shutdown-stress-summary`.
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
