# MISSION_RESULT — V5.2: calculation scope integrity and date-window semantics

## 1. Repository identity

| fact | value |
|---|---|
| repo | `asgareyvazi/Prog-Proc` |
| branch | `arena/01a0c936-prog-proc` |
| local HEAD | `6c6d900` |
| remote HEAD | `6c6d900` — verified by `git ls-remote` |
| V5.1 baseline | `0b5b3da` (confirmed present, `git cat-file -e`) |
| merge-base | `5fc4c85` reaches HEAD; **0 merge commits** in `5fc4c85..HEAD` |
| worktree | clean, `git status --porcelain` = 0 lines |

**The environment had been reset again at session start** (eighth time): HEAD was back at grafted
`e862113`, 86 uncommitted paths, `.venv` absent, shallow clone — while the remote still held
`0b5b3da`. Recovery was proven before any work: re-provisioned `.venv`, `git fetch --depth=60`,
staged everything and diffed against `FETCH_HEAD` → **zero differences**, all **38 untracked files
byte-identical** to their remote versions, local **0 ahead / 23 behind**. Unlinked only the proven
identical files, `git checkout FETCH_HEAD -- .`, `git merge --ff-only` → HEAD `0b5b3da`, clean.
Nothing was discarded.

## 2. Previous mission verification

V5.1's claims were re-proved from the repository, not accepted:

* lineage is **linear** from `5fc4c85`; all four cited commits present as objects;
* the V5.1 **code** is present — `sole_problem_on_event` / `having(count == 1)` in `field.py`,
  `NAME_AMBIGUOUS` / `matched_by` in `engineering/repository.py`;
* the V5.1 **tests** are present (2 NPT-attribution tests, `TestMatchProvenance`);
* all three affected suites re-run green: **118 tests, exit 0**.

## 3. Defects found

### 3.1 `CALC_SCOPE_BYPASS` — confirmed defect, fixed

**Symptom.** A calculation could be persisted with a scope contradicting the hierarchy it points at.

**Minimal reproduction** (measured before the fix):

| attempt | result |
|---|---|
| `well_id=A, section_id=` *(section of well B)* | **ACCEPTED and persisted** |
| `section_id=` *(no `well_id`)* | **ACCEPTED and persisted** |
| `well_id=A, project_id=` *(project of another block)* | **ACCEPTED and persisted** |
| invented `well_id` / `section_id` / `project_id` | raw `sqlite3.IntegrityError` |

**Root cause.** `EngineeringRepository._check_scope()` exists and is called by `create_program`,
`create_procedure` and the section writer (lines 338, 444, 771) — but **not** by
`record_calculation()`, which copied `well_id` / `section_id` / `project_id` straight into the
content dict.

**Semantic contract.** The same contradiction must be refused by every writer in the repository. It
matters most for calculations because a calculation is the row a later reader is most likely to
treat as an authoritative engineering number.

**Fix.** Route `record_calculation`'s scope through `_check_scope` before the content dict is built —
the smallest root-cause fix, reusing the existing validator rather than duplicating it. All six
attempts are now domain `ValidationError`s, and a consistent scope still writes.

**Regression tests.** 5, in a new `TestCalculationScopeIntegrity`. **Mutation M3** (delete the new
call) is **killed by 4 tests**.

### 3.2 Undocumented date-window semantics — contract gap, fixed

The behaviour itself is correct (see §4.2), but the rule "an undated row survives a date window" was
written down **nowhere** — an implicit semantic at a correctness boundary. It is now stated on both
the shared filter (`search/index.py`) and the public `RetrievalRequest`, and pinned by two tests.

## 4. Hypotheses disproved

### 4.1 `DATE_SCOPE_UNDATED_SEARCH_RETRIEVAL` — **DISPROVED as a silent leak**

Attacked exactly as specified. An early probe appeared to show a dated-but-out-of-range record
surviving; that was **my probe's fault** — `LessonLearned` has no `record_date` column, so the
attribute I set never reached the index. Re-run with a type that genuinely carries one
(`ProblemOccurrence.occurred_at`):

```
no window  -> 3 items: 2024-01-15, 2025-06-10, undated
June 2025  -> 2 items: 2025-06-10, undated
```

The window **does** exclude what it can disprove. What it cannot disprove it keeps, and the item
comes back with `record_date == ""`. That is the platform's standing rule that an empty date means
*unknown*, not "outside every window"; dropping an undated problem from "June 2025" would assert the
source never stated. §26's forbidden case — returning the row *without telling the caller* — does not
occur, because the empty date is on the item. Recorded as disproved, with the contract documented.

## 5. Mandatory attack register (§80)

| attack ID | result | evidence |
|---|---|---|
| `CALC_SCOPE_BYPASS` | **DEFECT_FIXED** | 3 contradictions persisted; now `ValidationError`; 5 tests; M3 killed by 4 |
| `DATE_SCOPE_UNDATED_SEARCH_RETRIEVAL` | **HYPOTHESIS_DISPROVED** | out-of-range excluded, undated returned with `record_date=""`; contract documented, 2 tests |
| `CALC_SUPERSESSION_DOUBLE_LEAF_RACE` | **OPEN / NOT EXERCISED** | — |
| `PLAN_EXPLICIT_TEMPLATE_BY_NAME` | **OPEN / NOT EXERCISED** | — |
| `PLAN_EXACT_ID_COLLISION` | **OPEN / NOT EXERCISED** | — |
| `SCOPE_NULL_HIERARCHY` | **PARTIAL** | null-hierarchy permutations not built; `section` without `well` **is** now refused |
| `REVIEW_NULL_WELL_LEAK` | **OPEN / NOT EXERCISED** | — |
| `TIMELINE_INTERVAL_WINDOW` | **OPEN / NOT EXERCISED** | — |
| `RETRIEVAL_ZERO_LIMIT_HARD_CAP` | **OPEN / NOT EXERCISED** | — |
| `EVIDENCE_IDENTITY_ORDER_INDEPENDENCE` | **OPEN / NOT EXERCISED** | — |
| `EVIDENCE_FRESHNESS_DELTA` | **OPEN / NOT EXERCISED** | — |
| `CITATION_NOT_CHECKABLE_SEMANTICS` | **OPEN / NOT EXERCISED** | — |
| `PROMOTION_ATOMIC_LATE_FAILURE` | **OPEN / NOT EXERCISED** | — |
| `CHILD_ROW_IDENTITY_DUPLICATION` | **OPEN / NOT EXERCISED** | — |
| `LIMIT_TRUNCATION_SIGNAL` | **OPEN / NOT EXERCISED** | — |

Two of fifteen mandatory attacks were carried to a verdict. The remaining thirteen are named here as
unexercised rather than implied by a green suite.

## 6. Mutation matrix

| mutation | target | result |
|---|---|---|
| M3 — remove `_check_scope` from `record_calculation` | calculation scope | **KILLED** (4 tests fail) |

Attempted **1**, killed **1**, survived **0**. The V5.1 mutations (fan-out, snapshot freeze,
staleness widening, programme scope, ambiguity signalling) were re-verified green via their tests but
were not re-introduced this session.

## 7. Verification gates

```
pytest (full)                 exit 0   1542 passed, 3 skipped, 0 failed, 0 errors  (1545 collected)
ruff check src tests migrations        exit 0   All checks passed!
ruff format --check src tests migrations exit 0  209 files already formatted
python -m compileall -q src tests migrations     exit 0
git diff --check                               exit 0
alembic heads                                  0011 (head)
alembic current                                no stamped revision (fresh DB, no version table)
```

1545 = V5.1's 1538 + the 7 tests added here. Counts come from a regex over the progress lines,
because `pytest -q` under a redirect emits no summary line in this environment.

## 8. Git publication

```
6c6d900  V5.2 DATE_SCOPE_UNDATED_SEARCH_RETRIEVAL   <- HEAD, remote-verified
23d994a  V5.2 CALC_SCOPE_BYPASS
0b5b3da  <- V5.1 baseline
```

Files changed this session: `engineering/repository.py` (+10), `search/index.py` (+14 docstring),
`retrieval/contract.py` (+5 docstring), `test_calculation_forensics.py` (+5 tests),
`test_retrieval_forensics.py` (+2 tests). No generated files, no debug code, no temporary probes
left behind (three probe files were created under `tests/integration/test_zz_*.py` and deleted before
committing; `git status` is clean).

## 9. Final verdict

**Certifiable with explicitly bounded remaining scope.**

Certified by executable evidence this session: repository lineage and recovery; V5.1 invariant
re-proof; calculation scope integrity (`CALC_SCOPE_BYPASS`); date-window semantics across search and
retrieval. Every gate that was run is green and the work is remotely verified.

**Not certified**, because it was not exercised: the other thirteen mandatory attacks in §5, and with
them calculation provenance/identity/revision/dependency (§5.2–§11), timeline (§15–§18), review
current/history/scope/limits (§19–§24), evidence and citation (§34–§40), promotion contracts and
child-row identity (§41–§49), corrupted-data reads (§50), transaction and savepoint atomicity
(§55–§56), concurrency (§57–§58), performance (§59) and the query-limit theory audit (§60–§61). A
green suite does not cover them and this report does not claim that it does.
