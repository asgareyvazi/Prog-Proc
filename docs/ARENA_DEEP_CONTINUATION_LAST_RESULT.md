# MISSION_RESULT — V5.3: checkpointed forensic closure

This report is updated at every checkpoint and committed with the work it describes.

## 1. Repository identity

| fact | value |
|---|---|
| repo | `asgareyvazi/Prog-Proc` |
| branch | `arena/01a0c936-prog-proc` |
| HEAD at session start | `c65f8ad` |
| remote tip at session start | `c65f8ad` — verified by `git ls-remote` |
| worktree at start | clean (0 paths) |
| shallow | false |

## 2. Environment recovery

**No reset this session.** HEAD already equalled the remote tip and the worktree was clean, so no
recovery was needed. The protocol was still run first (`branch`, `status`, `rev-parse`,
`is-shallow-repository`, `fetch`, `ls-remote`) before any file was touched.

## 3. Previous V5.2 verification

Re-proved from the repository, not from the prior report: `_check_scope` present (6 references) in
`engineering/repository.py`; `TestCalculationScopeIntegrity` and `TestDateWindowSemantics` both
present; both suites re-run green (**75 tests, exit 0**).

## 4. Defects found

### 4.1 `CALC_SUPERSESSION_DOUBLE_LEAF_RACE` — confirmed, fixed

**Symptom.** Two independent sessions could both supersede the same parent calculation, leaving a
chain with **two current leaves**.

**Reproduction.** Two threads, two `db.unit_of_work()` sessions, one `threading.Barrier`, both
calling `record_calculation(supersedes_id=parent)` with different content. Measured over 8 runs:

```
PROBE DIFFERENT content: 7/8 runs produced TWO leaves
   children_of_parent=2  parent_status=SUPERSEDED
   identities=['calc:20e8eb3', 'calc:6c44edc']
```

**Root cause.** `record_calculation` guards supersession with an application pre-check —
`select(Calculation.id).where(supersedes_id == parent.id).limit(1)`, then raise if found. That is a
check-then-act: both writers read "no child yet" before either writes. `supersedes_id` carried only
a foreign key, and the two children had different `identity_key`s, so the existing unique index did
not catch it either. Nothing on either row said which revision to follow.

**Semantic contract.** A calculation chain has exactly one leaf. ADR-0003 puts multi-writer
concurrency out of scope, but "out of scope" describes what the system supports — it does not make a
silently forked chain acceptable, and the corruption is persistent.

**Fix.** The invariant moves to the database as a **partial unique index** on
`calculation.supersedes_id` (`WHERE supersedes_id IS NOT NULL`) — migration **0012**, plus the
matching `Index(...)` on the ORM model so `create_all` and the migration agree. This is the same
mechanism `uq_document_version_one_current` and the programme/procedure current-revision indexes
already use. No application-level check can close a gap between a read and a write; only the
database sees both. `METADATA_REVISION` bumped `0011` → `0012`, which its own docstring requires.

**Result after the fix.** `0/8 runs produced TWO leaves`. The loser now fails on a constraint or on
the pre-check, depending on timing; either way exactly one child survives.

**Regression tests.** 3, in `TestOneSupersedingRevision`:
the real threaded race with independent sessions; a deterministic direct test that inserts a second
child by raw SQL with the application layer entirely bypassed and expects `IntegrityError`; and a
`v1 → v2 → v3` walk proving the constraint does not break ordinary revision.

**Mutation.** Dropping the index from the ORM is **killed** by the direct test. Honest note: on that
run the *threaded* test still passed, because the pre-check happened to win the timing — which is
exactly why the deterministic direct test exists alongside it.

**Migration verification.** `alembic heads` → **`0012`**; `tests/integration/test_migrations.py`
**10 tests, exit 0**, including the head-constant assertion and the wheel-without-scripts stamping
path (both initially failed until `METADATA_REVISION` was bumped, then passed).

## 5. Hypotheses disproved

None newly this checkpoint. V5.2's `DATE_SCOPE_UNDATED_SEARCH_RETRIEVAL` remains disproved with its
contract documented.

## 6. Calculation certification

| item | status |
|---|---|
| `CALC_SCOPE_BYPASS` (V5.2) | **PASS_WITH_BEHAVIOURAL_PROOF** — re-run green |
| `CALC_SUPERSESSION_DOUBLE_LEAF_RACE` | **DEFECT_FOUND_AND_FIXED** |
| revision chain `v1→v2→v3`, current leaf, history retention | **PASS_WITH_BEHAVIOURAL_PROOF** |
| provenance / identity / input indexing / `calculations_using` / `calculation_impact` | **PASS** via existing suite (74 tests) — **not newly attacked this checkpoint** |
| numeric safety, actor/trigger and identity-key semantics | **OPEN_NOT_EXERCISED** for new attacks |

## 19. Mutation matrix (running)

| mutation | target | expected to fail | actual | status |
|---|---|---|---|---|
| remove calc scope enforcement (V5.2 M3) | calculation | scope tests | 4 failures | **KILLED** |
| drop `uq_calculation_one_superseding_revision` | calculation | constraint test | 1 failure | **KILLED** |

## 20. Full test gates

| gate | result |
|---|---|
| `ruff check src tests migrations` | exit 0 |
| `ruff format --check src tests migrations` | exit 0, 210 files |
| `python -m compileall -q src tests migrations` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012` |
| targeted: calculation + change-impact + migrations | exit 0, 83 tests |
| full suite | see the latest checkpoint block below |

## 21. Git publication checkpoints

```
CHECKPOINT A — CALCULATION
status: COMPLETE
targeted_tests: test_calculation_forensics + test_change_impact_forensics + test_migrations = 83, exit 0
mutations: 1 attempted, 1 killed
remaining_open: numeric safety, identity-key semantics, actor/trigger attacks
```

## 22. Remaining open scope

Carried-forward mandatory attacks still open after Checkpoint A:

```
PLAN_EXPLICIT_TEMPLATE_BY_NAME      PLAN_EXACT_ID_COLLISION
REVIEW_NULL_WELL_LEAK               TIMELINE_INTERVAL_WINDOW
RETRIEVAL_ZERO_LIMIT_HARD_CAP       LIMIT_TRUNCATION_SIGNAL
EVIDENCE_IDENTITY_ORDER_INDEPENDENCE  EVIDENCE_FRESHNESS_DELTA
CITATION_NOT_CHECKABLE_SEMANTICS    PROMOTION_ATOMIC_LATE_FAILURE
CHILD_ROW_IDENTITY_DUPLICATION      SCOPE_NULL_HIERARCHY
```

## Final verdict

In progress — see the checkpoint blocks above. The current verdict is
**CERTIFIABLE WITH BOUNDED REMAINING SCOPE** for the calculation supersession surface only; the
twelve attacks listed in §22 are unexercised.
