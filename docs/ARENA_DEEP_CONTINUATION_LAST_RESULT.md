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

### 4.2 `PLAN_EXACT_ID_COLLISION` — confirmed, fixed

**Symptom.** Two current governing programmes both carried a target on the same `section_id` with
different planned depths. The summary silently reported one of them, labelled `matched_by=SECTION_ID`.

**Reproduction.** Programmes A and B, both well-owned (therefore both governing), both targeting
section `coll-x`, planned 1000.0 and 2000.0. Measured:

```
PROBE exact-ID collision -> planned=2000.0 matched_by=SECTION_ID
```

**Root cause.** V5.1's ambiguity flag covered only *name* matches, on the reasoning that an id join
is exact. That is true of the section and says nothing about how many plans claim it — an exact-id
collision is the stronger disagreement, because neither side is guessing.

**Fix.** `_match_target` collects every target whose `section_id` matches and reports
`SECTION_ID_AMBIGUOUS` when there is more than one. The precedence order still picks one, so the
answer stays deterministic; it just says that it picked. **Regression tests:** 2.
**Mutation** (never report the ambiguous label): **killed**, 1 failure.

### 4.3 `PLAN_EXPLICIT_TEMPLATE_BY_NAME` — confirmed, fixed

**Symptom.** Naming a field template explicitly returned *nothing*, while not naming it returned the
template's plan.

**Reproduction.** Field-level programme, unbound target (`section_id=None`), section named to match:

```
PROBE [well_id only]      -> planned=[5000.0] matched_by=['NAME']
PROBE [program_id only]   -> rows=0
PROBE [well + program]    -> rows=0
PROBE [section + program] -> rows=0
```

**Root cause.** When `program_id` is supplied and the programme has no well, the section side was
narrowed to the sections its targets point at *explicitly*. An unbound target points at none, so the
intersection was empty. The caller's own well/section anchor was discarded in favour of a narrower
one.

**Fix.** When the caller has already anchored the section side with `well_id` or `section_id`, that
anchor is the boundary and the programme's targets no longer narrow it; unbound targets stay
eligible to match by name exactly as when no programme is named. With `program_id` **alone** and no
anchor, the answer is still empty — a field template with unbound targets has no actuals to compare
against, and widening to every well in the workspace is the cross-well bug this code exists to
prevent. **Regression tests:** 4. **Mutation** (restore the unconditional narrowing): **killed**, 2
failures. The existing cross-well protection tests still pass.

### 4.4 `RETRIEVAL_ZERO_LIMIT_HARD_CAP` — confirmed, fixed

**Symptom.** `limit=0` is documented as "no cap" but returned *fewer* rows than a large explicit
limit on the same corpus.

**Reproduction.** 260 matching authoritative records in one well:

```
PROBE RETRIEVAL limit=   0 -> items=200
PROBE RETRIEVAL limit= 200 -> items=200
PROBE RETRIEVAL limit=1000 -> items=260
```

**Root cause.** `cap = req.limit if req.limit > 0 else _DISCOVERY_CAP`, with a private
`_DISCOVERY_CAP = 200`. The output list was correctly uncapped, but discovery had already thrown the
rest away, and nothing in the bundle said a bound had been applied. Asking for everything got less
than asking for a lot.

**Fix.** Discovery now inherits the search layer's own `MAX_CANDIDATES` (4000) — the platform's
real, already-documented bound, with its own `truncated` signal — instead of a private number, and
`EvidenceBundle` gains `discovery_capped` so a short answer is distinguishable from a short answer
that ran out of room. After the fix `limit=0` returns **260**, equal to `limit=1000`. The
`limit=0` docstring now says precisely what is and is not bounded. **Regression tests:** 2.
**Mutation** (restore the 200 cap): **killed**, 1 failure.

### 4.5 Three migration tests pinned 0011 as the head — stale assertions, fixed

Migration 0012 turned three assertions red. None was a product defect; each was pinning the same
thing — *"this migration is still the head"* — instead of anything 0011 actually guarantees:

1. `test_the_revision_is_a_real_head_of_a_single_headed_chain` asserted
   `METADATA_REVISION == "0011"`. True of exactly one revision at a time, so it would break on
   every future migration for no gain. It now asserts the invariant that outlives a single head:
   the chain is single-headed, `METADATA_REVISION` tracks the real head, and 0011 is still an
   addressable revision in it.
2. `test_the_upgrade_is_replayable` asserted `upgrade(engine, "0011").up_to_date is True`.
   `up_to_date` is literally `current == head` — it means *at the head of the chain*, not *at the
   revision I asked for*. Sitting at 0011 is now genuinely a revision behind, and **reporting that
   is the point of the field**: a workspace must be able to say it is stale. Replay now asserts
   what replaying actually promises — a row-level snapshot is unchanged — rather than conflating
   "replayed" with "at head".
3. `test_migration_0005` parity upgraded only to `"0005"`. Parity is claimed for a *workspace*, and
   a workspace is at the head. Against `"0005"` the comparison would have passed even with a later
   migration and the models disagreeing about `calculation` — precisely the drift the test exists to
   catch. Upgrading to `head` makes it stronger, and it now holds **with 0012's index included**,
   which independently confirms the migration and the ORM `Index` produce the same object.

These are assertion corrections, not weakenings: all three now assert more, not less. Migration
tests after the change: **26 passed**.

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

## 18. Section-by-section audit (§48 form)

> **Historical — superseded by §25.** The `#` column below is this section's own enumeration and is
> *not* the master ledger's row number: rows 16/17/18 here are review and retrieval subjects, while
> rows 16/17/18 in §25 are the search-boundary subjects. Kept as the record of what was audited at
> the time; for a row's current status, §25 is the only authority.

Verdicts use the six labels only. A label is never `PASS` merely because a suite stayed green.

| # | Area | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Calculation scope enforcement | `PASS_WITH_BEHAVIOURAL_PROOF` | V5.2 `_check_scope` on the recording path re-proved; 5 tests green |
| 2 | Calculation supersession, sequential | `PASS_WITH_BEHAVIOURAL_PROOF` | 59 pre-existing tests green before and after migration 0012 |
| 3 | Calculation supersession, concurrent | `DEFECT_FOUND_AND_FIXED` | 7/8 double leaves → 0/8; 3 tests; mutation killed |
| 4 | Revision matrix: scope/method mutated mid-revision | `OPEN_NOT_EXERCISED` | no fixture varies scope or method across revisions |
| 5 | Provenance, `calculations_using`, `calculation_impact` | `OPEN_NOT_EXERCISED` | read paths exist, not audited |
| 6 | Calculation identity key | `PASS_WITH_BEHAVIOURAL_PROOF` | distinct `identity_key` on the competing v2 is what let two leaves through |
| 7 | Numeric safety | `OPEN_NOT_EXERCISED` | outside the attacks closed here |
| 8 | Plan-vs-actual matching | `DEFECT_FOUND_AND_FIXED` | 2 attacks, 6 tests, 2 mutations killed |
| 9 | Plan-vs-actual scope hierarchy | `DEFECT_FOUND_AND_FIXED` | `program_id` alone still deliberately returns nothing |
| 10 | NPT attribution, field intelligence | `PASS_WITH_BEHAVIOURAL_PROOF` | V5.1 sole-problem rule green |
| 11 | Timeline: point events vs intervals | `OPEN_NOT_EXERCISED` | 10 interval cases still unrun |
| 12 | Timeline: malformed interval bound | `OPEN_NOT_EXERCISED` | not probed |
| 13 | Timeline: record-date provenance | `PASS_WITH_BEHAVIOURAL_PROOF` (bounded) | V5.2 date-window suite green |
| 14 | Timeline: determinism on identical timestamps | `OPEN_NOT_EXERCISED` | the test does not exist |
| 15 | Review null-well semantics | `OPEN_NOT_EXERCISED` | not probed |
| 16 | Review truncation / sub-repository completeness | `OPEN_NOT_EXERCISED` | not probed |
| 17 | Review current-vs-history matrix | `OPEN_NOT_EXERCISED` | not probed |
| 18 | Retrieval `limit=0` | `DEFECT_FOUND_AND_FIXED` | 200 vs 260, now equal; 2 tests; mutation killed |
| 19 | Search `limit=0` | `HYPOTHESIS_DISPROVED` | `int(limit or default_limit)` makes 0 mean "use the default", not "cap at 200" |
| 20 | Retrieval truncation signal | `DEFECT_FOUND_AND_FIXED` | `discovery_capped` reports the only remaining bound |
| 21 | Search candidate-cap semantics | `OPEN_NOT_EXERCISED` | `truncated = len(hits) > MAX_CANDIDATES` is a corpus-size statement, not a per-query one |
| 22 | Evidence identity / freshness / citation semantics | `OPEN_NOT_EXERCISED` | not probed |
| 23 | Promotion atomicity on late failure | `OPEN_NOT_EXERCISED` | not probed |
| 24 | Child-row identity | `OPEN_NOT_EXERCISED` | not probed |
| 25 | Scope null hierarchy | `OPEN_NOT_EXERCISED` | not probed |
| 26 | Corruption / defensive reads | `OPEN_NOT_EXERCISED` | not probed |
| 27 | Performance at 10k | `OPEN_NOT_EXERCISED` | not run |
| 28 | System-wide limit audit | `OPEN_DEFECT` | retrieval now coherent; search, review and CLI limits not yet reconciled |
| 29 | Migration/ORM alignment | `PASS_WITH_BEHAVIOURAL_PROOF` | 0012 head, `METADATA_REVISION` bumped, 10 migration tests green |
| 30 | Gates after the last change | `PASS_WITH_BEHAVIOURAL_PROOF` | ruff 0, format 0, compileall 0, `git diff --check` 0 |

## 19. Mutation matrix (running)

| mutation | target | expected to fail | actual | status |
|---|---|---|---|---|
| remove calc scope enforcement (V5.2 M3) | calculation | scope tests | 4 failures | **KILLED** |
| drop `uq_calculation_one_superseding_revision` | calculation | constraint test | 1 failure | **KILLED** |
| never report `SECTION_ID_AMBIGUOUS` | plan | collision test | 1 failure | **KILLED** |
| restore unconditional section narrowing | plan | template-scope tests | 2 failures | **KILLED** |
| restore the private 200 discovery cap | retrieval | zero-limit test | 1 failure | **KILLED** |

## 20. Full test gates

| gate | result |
|---|---|
| `ruff check src tests migrations` | exit 0 |
| `ruff format --check src tests migrations` | exit 0, 210 files |
| `python -m compileall -q src tests migrations` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012` |
| targeted: calculation + change-impact + migrations | exit 0, 83 tests |
| full suite (after the last change) | **1553 passed / 3 skipped / 1556, 0 failed** |
| targeted: migration 0005 + 0011 + migrations | exit 0, 26 tests |

## 21. Git publication checkpoints

```
CHECKPOINT D' — MIGRATION TEST PINS
status: COMPLETE
attacks_closed: none (stale test assertions uncovered by the full suite after 0012)
targeted_tests: test_migration_0005 + test_migration_0011 + test_migrations = 26, exit 0
mutations: n/a
full_suite_at_checkpoint: 1553 passed / 3 skipped / 1556, 0 failed
remaining_open: LIMIT_TRUNCATION_SIGNAL and the §22 list

CHECKPOINT D — RETRIEVAL LIMITS
status: COMPLETE
attacks_closed: RETRIEVAL_ZERO_LIMIT_HARD_CAP (fixed)
targeted_tests: retrieval + evidence_package + evidence_citation = 98, exit 0; plus 2 new zero-limit tests
mutations: 1 attempted, 1 killed
remaining_open: LIMIT_TRUNCATION_SIGNAL (search candidate-cap semantics)

CHECKPOINT B — PLAN-VS-ACTUAL
status: COMPLETE
attacks_closed: PLAN_EXACT_ID_COLLISION (fixed), PLAN_EXPLICIT_TEMPLATE_BY_NAME (fixed)
targeted_tests: test_plan_actual_forensics 38, exit 0; plus engineering_lessons + cli_domain = 100, exit 0
mutations: 2 attempted, 2 killed
remaining_open: none for plan-vs-actual

CHECKPOINT A — CALCULATION
status: COMPLETE
targeted_tests: test_calculation_forensics + test_change_impact_forensics + test_migrations = 83, exit 0
mutations: 1 attempted, 1 killed
remaining_open: numeric safety, identity-key semantics, actor/trigger attacks
```

## 22. Remaining open scope

Carried-forward mandatory attacks still open after Checkpoint A:

```
REVIEW_NULL_WELL_LEAK               TIMELINE_INTERVAL_WINDOW
LIMIT_TRUNCATION_SIGNAL
EVIDENCE_IDENTITY_ORDER_INDEPENDENCE  EVIDENCE_FRESHNESS_DELTA
CITATION_NOT_CHECKABLE_SEMANTICS    PROMOTION_ATOMIC_LATE_FAILURE
CHILD_ROW_IDENTITY_DUPLICATION      SCOPE_NULL_HIERARCHY
```

## Final verdict

**CERTIFIABLE WITH BOUNDED REMAINING SCOPE.**

*Correction carried into V5.4:* the sentence below said "the ten attacks in §22".  §22 names
**nine**.  The register opened with 13 attacks; 3 were resolved before Checkpoint D, leaving 10, and
Checkpoint D resolved `RETRIEVAL_ZERO_LIMIT_HARD_CAP` — §22 was updated to nine but this sentence was
not.  Nine is correct.  Separately, §22 is *not* the complete open set: §18 independently lists 17
open surfaces (16 `OPEN_NOT_EXERCISED` + 1 `OPEN_DEFECT`), of which only 9 carry a named attack id.
The master ledger in §24 is now the single authoritative enumeration.

Four attacks are closed with behavioural proof, not with a green suite:
`CALC_SUPERSESSION_DOUBLE_LEAF_RACE`, `PLAN_EXACT_ID_COLLISION`,
`PLAN_EXPLICIT_TEMPLATE_BY_NAME`, `RETRIEVAL_ZERO_LIMIT_HARD_CAP`. Five mutations were attempted and
five were killed. The full suite after the last change is 1553 passed / 3 skipped / 1556 with zero
failures.

What is *not* certified, and must not be read as certified: the ten attacks in §22 are
`OPEN_NOT_EXERCISED`, and the system-wide limit audit (§28) is an `OPEN_DEFECT` — retrieval is now
internally coherent, but search, review and CLI limits have not been reconciled against it. This
verdict covers the calculation supersession, plan-vs-actual and retrieval-limit surfaces only.

---

# V5.4 — Checkpointed Forensic Closure and System-Wide Certification

## 24. V5.4 repository identity and recovery

| item | value |
| --- | --- |
| repository | `asgareyvazi/Prog-Proc` |
| branch | `arena/01a0c936-prog-proc` |
| mission-named remote baseline | `dd20f71e11ee30419aa21c370c97eebe40042835` |
| remote tip at mission start (`git ls-remote`) | `dd20f71e11ee30419aa21c370c97eebe40042835` — **matches** |
| local HEAD at mission start | `e8621136ca73108ae7b590e6baa72fedc1f00835` — **the branch point, not the tip** |
| shallow | `true` (single grafted commit at start) |
| recovery required | **yes** |

### 24.1 The recovery event

The session opened with Git metadata reverted to the branch point `e862113` while the worktree still
carried all V4/V5 work, so the entire history appeared as ~100 uncommitted paths. This is the eighth
occurrence of this failure mode.

Recovery was lossless and evidence-led, per the standing rule never to discard files to make the
repository look tidy:

1. The local report and `git status` were copied to a scratch directory **before** any recovery
   action, so nothing could be lost.
2. `git fetch --depth=60 origin arena/01a0c936-prog-proc` → `FETCH_HEAD = dd20f71`.
3. Every differing path was byte-compared against `FETCH_HEAD:<path>` with `cmp`. Exactly **one**
   path differed: `docs/ARENA_DEEP_CONTINUATION_LAST_RESULT.md` (47 insertions / 4 deletions).
   Everything else in the worktree was already byte-identical to the remote tree.
4. `git merge-base --is-ancestor e862113 FETCH_HEAD` → true, so a fast-forward was valid.
5. `git checkout FETCH_HEAD -- .` then `git merge --ff-only FETCH_HEAD` → HEAD `dd20f71`, then the
   preserved report was restored.

**Local-only work found and preserved:** the V5.3 report finalisation (commit `724c189`), which had
never been pushed. `git cat-file -e` confirms `724c189` exists in neither the local object database
nor the remote — the V5.3 statement that it was `LOCAL_COMMIT_ONLY` was correct. Its content survived
only because the worktree persisted; it is republished here.

`.venv` had also vanished and was re-provisioned: Python 3.11.2, ruff 0.16.9, pytest 9.1.1,
`alembic heads` = `0012`.

### 24.2 Baseline re-proof at `dd20f71`

Every claimed V5.3 change was verified **in the tree**, not from a report:

| claimed change | verified at |
| --- | --- |
| migration 0012 partial unique index | `migrations/versions/20260927_0012_one_superseding_calculation.py:37,41,42` |
| matching ORM `Index` | `database/models.py:696` |
| retrieval inherits `MAX_CANDIDATES` | `retrieval/service.py:72,109,255` |
| `EvidenceBundle.discovery_capped` | `retrieval/contract.py:243,256` |
| plan exact-id ambiguity | `engineering/repository.py:1316` |
| explicit-template anchor branch | `engineering/repository.py:1163` |
| migration test corrections | `test_migration_0011.py:67`, `test_migration_0005.py:240` |

Targeted re-proof: **149 tests, 0 failures, exit 0** (calculation, plan-vs-actual, retrieval,
migrations, 0005, 0011, report integrity).

## 25. Master certification ledger

Reconstructed from the repository. Replaces §18 and §22 as the single enumeration; `#` is stable.

| # | Surface | Contract | Source | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Calculation scope enforcement | a recorded calculation's scope must match its well's hierarchy | `engineering/repository.py:273` | `BEHAVIOURALLY_PROVEN` |
| 2 | Supersession, sequential | one superseding revision per parent | `engineering/repository.py:1547` | `BEHAVIOURALLY_PROVEN` |
| 3 | Supersession, concurrent | the *database* enforces one child | migration 0012 | `DEFECT_FOUND_AND_FIXED` |
| 4 | Plan exact-id collision | an id join several plans claim is ambiguous | `engineering/repository.py:1316` | `DEFECT_FOUND_AND_FIXED` |
| 5 | Plan explicit template by name | a caller anchor is the scope boundary | `engineering/repository.py:1163` | `DEFECT_FOUND_AND_FIXED` |
| 6 | Retrieval `limit=0` | no-cap must not return fewer rows than a large explicit limit | `retrieval/service.py:109` | `DEFECT_FOUND_AND_FIXED` |
| 7 | Retrieval cap disclosure | the internal bound must be reported | `EvidenceBundle.discovery_capped` | `DEFECT_FOUND_AND_FIXED` |
| 8 | Search `limit=0` | 0 means "use the default", not a cap | `search/service.py:351` | `HYPOTHESIS_DISPROVED` |
| 9 | NPT attribution | an event's NPT belongs to its sole problem | `intelligence/field.py` | `BEHAVIOURALLY_PROVEN` |
| 10 | Date-window / undated | unknown date is not outside every window | `retrieval/contract.py` | `BEHAVIOURALLY_PROVEN` |
| 11 | Migration / ORM parity | fresh `create_all` equals migrated-to-head | `test_migration_0005.py:240` | `BEHAVIOURALLY_PROVEN` |
| 12 | **Review truncation truthfulness** | a review must not claim a cut it did not make | `review/service.py:1540` | **`DEFECT_FOUND_AND_FIXED`** (V5.4) |
| 13 | Review null-well semantics | null must never become an accidental wildcard | `review/service.py` `_for_well` | `OPEN_NOT_EXERCISED` |
| 14 | Review current/history matrix | one definition of currentness per domain | `review/service.py` | `OPEN_NOT_EXERCISED` |
| 15 | Review read-only proof | a read must not write | `test_domain_review.py` fingerprint | `BEHAVIOURALLY_PROVEN` (existing) |
| 16 | Search `truncated` semantics | two facts OR'd: discovery cap and result-set cut (§34A, §35A) | `search/index.py:696,736` | `OPEN_NOT_EXERCISED` — both bounds now exercised at the real constants and mutation-killed, but one boolean still cannot say *which* was reached, and `to_dict()` serialisation is untested (§35E) |
| 17 | FTS vs scan equivalence | the accelerator must not change the answer | `search/index.py` | `OPEN_NOT_EXERCISED` — a real divergence was found and fixed for document chunks (§35B), with parity proven at 16 001 rows and M3 killed; **structured records, mixed populations, phrase/drilling tokens and in-memory at scale remain unexercised** |
| 18 | Strict-query fallback metadata | broadening must describe the answer returned | `search/service.py` | `DEFECT_FOUND_AND_FIXED` |
| 19 | Retrieval `capped` inference | `>=` cap does not prove truncation | `retrieval/service.py` `_discover` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 20 | Evidence package cap visibility | a package must not hide a discovery ceiling | `evidence/contract.py:159`, `evidence/service.py:101` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 21 | Evidence identity order-independence | presentation must not change identity | `EvidenceQueryService._content_identity` | `OPEN_NOT_EXERCISED` |
| 22 | Evidence freshness delta | detect every mutation the contract promises | `check_freshness` | `OPEN_NOT_EXERCISED` |
| 23 | Citation `NOT_CHECKABLE` | "no citation" ≠ "citation failed" | `evidence/verify.py:59,116` | `BEHAVIOURALLY_PROVEN` (existing tests, see §26B) |
| 24 | Citation multi-aggregation | the worst citation decides the item | `evidence/verify.py:62,224` | `HYPOTHESIS_DISPROVED` (see §26B) |
| 25 | Promotion atomicity | late failure leaves no partial rows | `operations/promote.py:635`, `database/session.py:78` | `OPEN_NOT_EXERCISED` — tests written but **not proven sensitive**, see §26G |
| 26 | Child-row identity | no inference from UUID or row position | `operations/*.py` | `OPEN_NOT_EXERCISED` |
| 27 | Scope null hierarchy | unknown ≠ contradictory ≠ unbound | `engineering/repository.py:273` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 28 | Defensive reads / corruption | no silent repair of malformed persisted data | read paths, `doctor` | `OPEN_NOT_EXERCISED` |
| 29 | Timeline interval semantics | point-in-window over an index of dated facts, not a duration | `intelligence/timeline.py:326,341` | `BEHAVIOURALLY_PROVEN` — documented in §26F, boundary cases verified |
| 30 | Supplied dates must not become "no date" | a date given must be stored or refused | `operations/repository.py:112,133` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 31 | Timeline determinism | a total order on identical timestamps | `intelligence/timeline.py:121` | `OPEN_NOT_EXERCISED` — see §26F |
| 32 | CLI limit contract | the CLI must not re-implement limit policy | `cli/app.py:663`, `docs/LIMIT_CONTRACTS.md` §3 | `BEHAVIOURALLY_PROVEN` for `--limit 0` and cap disclosure; per-command negative-value handling still unaudited |
| 33 | Performance at 1k / 10k | bounded, no N+1 | all read paths | `OPEN_NOT_EXERCISED` |
| 34 | System-wide limit audit | one matrix, five distinct bound kinds | `docs/LIMIT_CONTRACTS.md` | `DEFECT_FOUND_AND_FIXED` (V5.4) |

Ledger row 34 is the reason five distinct limits must not be conflated; the constants actually in the
tree are `MAX_CANDIDATES = 4000` (`search/index.py:91`), `RETRIEVAL_CAP = MAX_CANDIDATES * 4`
(`search/index.py:100`), `_DISCOVERY_CAP = MAX_CANDIDATES` (`retrieval/service.py:109`),
`_SAFE_LIMIT = 10_000` (`review/service.py:95`), and `default_limit` (`search/service.py:193`, with a
derived `min(200, max(1, limit // 2))` at `:219`).

## 26. V5.4 defect: review claimed truncation it did not perform

**Contract.** `DomainReview.truncated` tells a consumer that the answer is incomplete. It is the only
signal a reviewer has that rows are missing.

**Symptom.** A review that returned every row it had reported `truncated=True`.

**Reproduction** (real corpus, real workspace, 5 mud reports × 22 measurements = 110):

```
PROBE limit=  100 child_bound=   500 truncated=True  records=90   <- nothing was cut
PROBE limit= 1000 child_bound=  5000 truncated=False records=90
```

**Root cause.** `review/service.py:1530` computed
`truncated = sections_truncated or any(size >= limit for size in bounded_sizes)`.
Three child batches — mud measurements (`:913`), BHA components (`:955`), survey stations (`:969`) —
are deliberately fetched at `.limit(limit * number_of_parents)` so one busy report cannot starve the
others. Their raw sizes were pushed into `bounded_sizes` and then compared against `limit`, a bound
belonging to a **different population**. With N parents the check trips whenever the child batch
returns ≥ `limit` rows, even though its own bound `limit * N` was nowhere near reached. The
generated corpus has one mud report, so `limit * 1 == limit` hid this completely.

**Fix.** `bounded_sizes` now holds `(size, bound)` pairs and a `note_bounded()` recorder takes the
bound that was actually applied to that query; the verdict compares each size against its own bound.
Every call site was routed through the recorder so no future site can bypass the pairing.

**A regression introduced and caught during the fix.** The first attempt routed the
`bounded_sizes.append(len(x))` sites through the existing `add_bounded()`, which also performs
`rows.extend(group)`. Those sites had already extended `rows` on their own line, so the rows were
added **twice** and the review grew from 90 records to 201. This was caught only because the record
count was checked rather than assumed; `grep -c 'rows.extend'` is now 6 in both the original and the
fixed file. The corrected fix separates recording from row collection.

**After the fix:**

```
PROBE limit=  100 child_bound=   500 truncated=False records=90
PROBE limit= 1000 child_bound=  5000 truncated=False records=90
```

`limit=10` still reports `truncated=True` (it genuinely cuts), so the true positive is intact.

**Regression tests:** 4 (`TestReviewTruncationTruthfulness`), including one asserting no record is
returned twice. **Mutations:** 2 attempted, **2 killed**.

## 26A. V5.4 defect: retrieval inferred a ceiling from a length

**Contract.** `EvidenceBundle.discovery_capped` says an uncapped request did not see the whole
population. It must not fire when discovery saw everything there was.

**Root cause.** Checkpoint D had computed it as `req.limit <= 0 and discovered >= _DISCOVERY_CAP`.
A corpus holding *precisely* `_DISCOVERY_CAP` (4000) matching rows satisfies that comparison while
discovery returned every row that existed, so the bundle invented a ceiling. The comparison also
discarded two facts the search layer already publishes: `SearchResponse.truncated` and
`SearchResponse.candidates` (`search/service.py:130,131`).

**Fix.** `_discover` now asks search for `cap + 1` rows when the caller imposed no cap, so only an
*extra* row proves there was more to find, and it ORs in search's own `truncated`, which covers the
search layer's independent candidate universe. Reaching the bound exactly is now reported as the
complete answer it is.

**Regression tests:** 2, at the real 4000-row boundary (built and retrieved in under 4 s each).

**A surviving mutation, and what was done about it.** The first mutation (restore `>= cap`
inference) was killed. The second (discard `response.truncated`) **survived**: no test exercised
search's own candidate cap. Rather than record a survivor, the gap was closed with
`test_the_search_layers_own_candidate_cap_is_propagated` at 4001 rows, after which the same mutation
is killed.

## 26B. V5.4 hypothesis disproved: citation aggregation is not inverted

A ledger row drafted during inspection claimed `_RANK` placed `NOT_CHECKABLE` **above** `MATCH`,
which would have made an unchecked citation outrank a verified one. That reading was wrong, and it
was wrong because it read the dictionary without reading how it is used.

`_RANK` (`evidence/verify.py:62`) is a *worst-is-lowest* scale, and the aggregation at `:224` is
`worst = min(results, key=lambda check: _RANK[check.status])`. So:

| citations on one item | aggregated status | why |
| --- | --- | --- |
| MATCH + MISMATCH | MISMATCH | rank 0 is the minimum |
| MATCH + UNREADABLE | UNREADABLE | rank 1 |
| MATCH + NOT_CHECKABLE | MATCH | rank 2 beats rank 3: a checked citation outranks an unchecked one |
| NOT_CHECKABLE only | NOT_CHECKABLE | never silently promoted to MATCH |

`all_verified` (`:116`) is `not (MISMATCH or UNREADABLE)`, and its docstring at `:100` states
explicitly that `NOT_CHECKABLE` does not block it because "an honest 'no file citation' is not a
broken citation", with the tally left for the reader. That is a documented contract, not a silent
collapse, so it is not changed.

Existing coverage already pins the surrounding matrix: `test_a_broken_second_citation_folds_the_row_to_the_worst`,
`test_a_structured_row_without_a_file_citation_is_not_checkable_not_passed`,
`test_a_citation_at_a_version_missing_from_the_registry_is_not_checkable`,
`test_an_item_without_any_provenance_is_not_checkable_not_passed`.

**Still honestly open:** the MATCH + NOT_CHECKABLE combination is *derived* from the code above but
has no test of its own, so it is proven by reading `min` and not by execution. It is recorded as
such rather than claimed as tested.

## 26C. V5.4 defect: the evidence package discarded the discovery ceiling

**Contract.** `TopicCoverage` exists so "an empty answer is a statement rather than a silence". Its
own docstring said every field is "read off the retrieval bundle for that topic, never re-derived".

**Defect.** The bundle carries `discovery_capped`, and the evidence layer never read it. A grep for
`capped` or `truncat` across `evidence/contract.py` and `evidence/service.py` returned **zero hits**.
A package consumer — or a CLI user reading text output — saw `260 returned, 0 dropped` with no way
to tell whether that was the whole answer or the point where discovery stopped. `broadened`, the same
kind of fact, was already carried.

**Fix.** `TopicCoverage.discovery_capped`, populated from `bundle.discovery_capped`, serialized in
`to_dict()`, and included in the identity payload. It belongs in the identity because `coverage`
already contributes `returned`/`dropped`/`broadened`: two packages with identical items but
different completeness are not the same answer, and `check_freshness` compares identity, so a newly
truncated read would otherwise have hashed identically to a complete one.

**CLI.** `command_evidence` printed `[broadened: ...]` and nothing about the cap, so the text output
could read as complete when it was not. Both notes are now emitted, and they can co-occur.

**Regression tests:** 4, including one that drives a real topic past the bound with 4001 rows and
one that runs the real CLI in both output modes. **Mutations:** 3 attempted, **3 killed**.

## 26D. V5.4 defect: a contract that lied about another layer

**Symptom.** `evidence/contract.py:76` stated that `limit` of zero means "no cap", **"the convention
the search and intelligence layers use"**.

**The claim was false of search.** `SearchService.search` resolves `int(limit or self.default_limit)`
(`search/service.py:351`), so a zero means *use the default*, not *everything*. Proven against a real
60-row corpus:

```
PROBE search.default_limit = 20
PROBE SEARCH    limit=  0 -> results= 20 candidates=61 truncated=False
PROBE RETRIEVAL limit=  0 -> items=60 discovery_capped=False
```

Search answered 20 while 61 candidates existed; retrieval answered all 60. The intelligence half of
the sentence was true — `lessons` resolves `.limit(limit if limit and limit > 0 else None)`
(`field.py:826`) and `offset_candidates` slices only for a positive limit (`:1002`) — so the defect
was one false clause in a sentence whose other half was right, which is the kind of error that
survives review.

**Why it matters.** A caller driving `SearchService.search` or `drillintel search --limit 0`
directly gets 20 rows and no signal that more existed. The evidence path is unaffected because
retrieval substitutes its own discovery bound rather than forwarding a zero.

**Fix.** The claim is corrected at the point of use, the real behaviour is documented where it lives
(`search/service.py:351`), and the whole per-layer contract is written down in
`docs/LIMIT_CONTRACTS.md` — five bound kinds, the `limit=0` meaning per layer, the CLI table, and
what each truncation flag does and does not claim. `tests/integration/test_limit_contracts.py` pins
all four layers plus the CLI, so the document and the behaviour cannot drift apart silently.

**Behaviour deliberately unchanged.** Making search treat zero as "everything" would turn a search
box into an unbounded read. The asymmetry is defensible; the undocumented claim about it was not.

**Regression tests:** 5. **Mutations:** 2 attempted, **2 killed**.

## 26E. V5.4 defect: a field and a project that contradicted each other were accepted

**Contract.** `_check_scope` exists to refuse "a record whose scope contradicts the hierarchy it was
copied from", because the scope columns are denormalised copies that nothing in the schema stops from
disagreeing.

**Probe.** Every case in the §15 matrix, against the real repository:

```
known+matching field               -> ACCEPTED
known+wrong field                  -> REJECTED: well W-known is not in field_id ...
known+matching project             -> ACCEPTED
known+wrong project                -> REJECTED: well W-known is not in project_id ...
well w/o field + explicit field    -> ACCEPTED      (correct: unknown, not contradictory)
well w/ nothing + explicit both    -> ACCEPTED
field+project CONSISTENT           -> ACCEPTED
field+project CONTRADICTORY        -> ACCEPTED      <-- DEFECT
unknown field id                   -> REJECTED: no field 'nope'
no scope at all                    -> ACCEPTED
```

**Root cause.** The well comparison and the field/project comparison are two different claims. The
well one asks "does this record's scope match the well it points at", and correctly skips when the
well's own value is NULL — a well whose field was never recorded is *unknown*, not *contradictory*.
But a field and a project named together make a claim about **each other** that needs no well at all:
`field.project_id` records the project the field belongs to. That claim was never checked, only the
existence of each id.

**Consequence.** A record could name field A beside project Bravo while the database said field A is
in project Alpha. Because the scope columns are denormalised *for querying*, that row then surfaces
in Bravo's report — the same hazard the docstring names, except it *includes* a field's records in
the wrong project rather than excluding a well from its own.

**Fix.** When both a field and a project are named and the field's `project_id` is known and
different, refuse. A field whose `project_id` is NULL stays acceptable, applying the same
unknown-is-not-contradictory rule the well check already uses, so the fix narrows nothing that was
legitimately unbound.

**Regression tests:** 6, covering the consistent, contradictory, field-NULL, well-NULL, unbound and
invalid cases, plus one proving the check is shared (a programme inherits it; `record_calculation`
cannot carry this contradiction because its scope parameters are `well_id`/`section_id`/`project_id`
with no `field_id`). Existing engineering suites: **189 tests, 0 failures** — nothing legitimate was
narrowed.

**Mutations:** 2 attempted, **2 killed**. One attempt at the second mutation silently failed to apply
(its guard assertion fired) and reported the previous mutation's failures; it was re-run with the
mutation verified present in the file before the tests, and it is the second run that is recorded.

## 26F. V5.4 timeline and date findings

### A supplied date was being laundered into "no date" — two ways, both silent

**Bare `date`.** `operations/repository._stamp` handled `datetime` and `str` but not
`datetime.date`. `date` is the *parent* of `datetime`, so `isinstance(value, datetime)` is False for
it and the value fell through to `return None`. Recording a problem with
`occurred_at=date(2025, 7, 1)` stored no timestamp at all. The timeline's own `_stamp`
(`intelligence/timeline.py:106`) *does* handle `date`, so the two readers disagreed.

This had propagated into a test: `test_moving_data_stales_a_snapshot_and_reports_the_difference`
asserted `set(differences) == {"occurrence_count"}` — that adding a July occurrence did **not** move
`last_seen_at`. That held only because the July date was being dropped. The assertion is corrected to
`{"occurrence_count", "last_seen_at"}`, which is what the data actually says; the test still asserts
staleness, the occurrence count, the absence of `well_count` and that the snapshot is not edited.

**Unreadable string.** A supplied-but-unparseable value also returned `None`, so
`ended_at="14 June 2025"` was stored as `ended_at IS NULL`: a mistyped end date became an operation
that never ended, with nothing saying a date had been dropped. The same file already refuses to do
this for a *query* bound — `_bound`'s docstring: "both look like an answer - so the caller is told
instead". A record is worse, because the silence is persisted. `_stamp_strict` now raises a domain
`ValidationError` at all five write sites; an *absent* date stays legal and prose still belongs in
the `*_text` columns.

**Regression tests:** 4 new. **Mutations:** 2 attempted, **2 killed**. The first attempt at the second
mutation matched its anchor twice and never applied, reporting a meaningless zero; it was re-run with
a verified-unique anchor, and that is the run recorded.

### Interval semantics: proven, not patched

Operations contribute two **point** entries, `<id>:start` and `<id>:end`. The consequence, probed:

```
operation spanning 10 -> 20 June, window 11 -> 19:  the operation does not appear
```

Neither endpoint falls in the window, so an operation in progress throughout it is absent. That is
correct under the module's stated contract — a timeline is "one ordered list built from the records
that already have dates", an index of dated facts, not a duration query, and the same docstring
refuses to invent a date for depth because "a timeline that invented one would be a story rather than
an index". Changing it to interval-overlap would invent exactly that. It is now documented and tested
rather than left implicit. Boundary cases verified: exact lower bound includes the start, exact upper
bound includes the end, a window after everything returns nothing, and `include_undated=True` restores
the undated tail inside a window.

### Determinism: an honest gap, not a claimed pass

`entry_comparator` sorts on `(dated, timestamp, kind, table, row_id)`. A well contributes one entry
per milestone and **every one carries the well's own id**, so two undated milestones of the same well
tie on every component:

```
kind=well table=well row_id=well-9798... at=None title='Spud: TL2-1'
kind=well table=well row_id=well-9798... at=None title='Completion: TL2-1'
```

A key that ties is not the total order the docstring claims; the emitted sequence comes from the order
`_WELL_EVENTS` is appended in. Adding `title` as a tiebreaker makes the key total but sorts
*Completion before Spud*, which is domain-wrong and broke a test that correctly asserts the lifecycle
order. That change was **reverted**: making the key total needs a domain ordinal the entry does not
currently carry, and shipping a domain-wrong order to satisfy a totality claim would be the worse
defect. Row 31 stays open with this reason recorded.

## 26G. V5.4 promotion atomicity: tested, but the tests are not proven to bite

`VersionPromoter` never commits; the caller owns the transaction and `unit_of_work` rolls back on
exception (`database/session.py:78`). Three tests were added against the dangerous shape — a
re-promotion with `replace=True`, which deletes this version's rows *before* rewriting them — forcing
a failure after the first write and comparing row counts before and after. All three **pass on the
real code**.

**Both mutations survived, and that is the result.**

| mutation | result | why |
| --- | --- | --- |
| remove `session.rollback()` from `unit_of_work` | **SURVIVED** (0 failures) | `session.close()` in the `finally` discards the uncommitted transaction anyway, so the explicit rollback is defensive rather than load-bearing |
| make `record_operation` commit mid-promotion | **SURVIVED** (0 failures) | not explained; a commit inside the caller's transaction should have persisted the first row and changed the counts |

The first survivor is understood and is itself useful: it says the rollback is not the thing holding
atomicity together. The second is **not** understood, and an unexplained survivor is not a pass. Row
25 therefore stays `OPEN_NOT_EXERCISED` — the behaviour looks right and is now watched, but nothing
here demonstrates that a regression would be caught. Claiming `BEHAVIOURALLY_PROVEN` on the strength
of three green tests whose mutations both survive would be exactly the false green this mission
exists to prevent.

## 27. V5.4 mutation matrix

| mutation | target | tests failing | result |
| --- | --- | --- | --- |
| restore `size >= limit` in the truncation verdict | `review/service.py:1540` | 2 | **KILLED** |
| record `limit` instead of the query's real bound | `review/service.py` `note_bounded` | 2 | **KILLED** |
| infer `discovery_capped` from `length >= cap` | `retrieval/service.py` `_discover` | 1 | **KILLED** |
| discard `SearchResponse.truncated` | `retrieval/service.py` `_discover` | 1 | survived first run, **KILLED** after the gap was closed |
| evidence layer drops `bundle.discovery_capped` | `evidence/service.py:101` | 1 | **KILLED** |
| identity payload ignores cap state | `evidence/service.py` `_content_identity` | 1 | **KILLED** |
| CLI hides the cap note | `cli/app.py:663` | 1 | **KILLED** |
| search treats `0` as no cap | `search/service.py:351` | 2 | **KILLED** |
| retrieval treats `0` as `20` | `retrieval/service.py` `_discover` | 1 | **KILLED** |
| remove the field/project consistency check | `engineering/repository.py:320` | 2 | **KILLED** |
| treat a NULL `field.project_id` as contradictory | `engineering/repository.py:322` | 1 | **KILLED** |
| bare `date` returns `None` again | `operations/repository.py` `_stamp` | 2 | **KILLED** |
| swallow an unreadable date again | `operations/repository.py` `_stamp_strict` | 1 | **KILLED** |
| remove `session.rollback()` from `unit_of_work` | `database/session.py:78` | 0 | **SURVIVED** — `close()` rolls back implicitly |
| a writer commits mid-promotion | `operations/repository.py` `record_operation` | 0 | **SURVIVED** — unexplained; row 25 left open |

## 28. V5.4 publication checkpoints

The per-checkpoint record lives in §30, which is the single authoritative list. This section held
the first draft of the Checkpoint 1 block; it was folded into §30 rather than left as a second copy
that could drift out of step with it.

## 29. V5.4 gate results

The full suite was run **after the last source and test change** of the mission, per the
no-false-green rule. No earlier suite result is reused.

| gate | command | result |
| --- | --- | --- |
| lint | `ruff check src tests migrations` | exit 0, "All checks passed!" |
| format | `ruff format --check src tests migrations` | exit 0, 210 files already formatted |
| compile | `python -m compileall -q src tests migrations` | exit 0 |
| whitespace | `git diff --check` | exit 0 |
| migration head | `alembic heads` | `0012 (head)` |
| report integrity | `pytest tests/unit/test_report_integrity.py` | exit 0, 5 passed |
| targeted: domain review | `pytest tests/integration/test_domain_review.py` | exit 0, 12 tests |
| targeted: review + CLI | review + cli_domain + cli | exit 0, 86 tests |
| targeted: retrieval + evidence | 3 suites | exit 0, 102 tests |
| baseline re-proof at `dd20f71` | 7 suites | exit 0, 149 tests |
| **full suite** | `pytest -q` | **1559 passed / 3 skipped / 1562 total, 0 failed, 0 errors** |

**How the full-suite total was calculated.** `pytest -q` again emitted no final summary line, so the
count was derived by summing the per-character progress output with
`^([.sFEx]+)\s*\[\s*\d+%\]$`, giving `total=1562 passed=1559 skipped=3 failed=0 errors=0`, with
`grep -cE '^FAILED'` = 0. This is consistent with the V5.3 baseline of 1556 plus the 6 tests added
in this mission (4 review truncation + 2 retrieval cap).

## 30. V5.4 publication checkpoints

```
checkpoint: V5.4-C1 — recovery, master ledger, review truncation truthfulness
status: REMOTE_PUBLISHED
local_head: 1f8cd1de75af0173f34ce0a74ceeb59bc4ec9ab6
remote_head: 1f8cd1de75af0173f34ce0a74ceeb59bc4ec9ab6
commit: 1f8cd1d
push: exit 0, dd20f71..1f8cd1d
remote_verified: yes - git ls-remote returned the same SHA as local HEAD
changed_files: review/service.py, test_domain_review.py, report
attacks_closed: review truncation truthfulness (ledger row 12)
tests: domain_review + cli_domain + cli = 86, exit 0; baseline re-proof = 149, exit 0
mutations: 2 attempted, 2 killed
remaining_open: ledger rows 13, 14, 16-18, 20-34

checkpoint: V5.4-C2 — retrieval discovery cap proven, not inferred
status: REMOTE_PUBLISHED
local_head: bc29467ecd5167e19287480f5238bf87462a6261
remote_head: bc29467ecd5167e19287480f5238bf87462a6261
commit: bc29467
push: exit 0, 1f8cd1d..bc29467
remote_verified: yes - git ls-remote returned the same SHA as local HEAD
changed_files: retrieval/service.py, test_retrieval_forensics.py, report
attacks_closed: retrieval discovery-cap inference (ledger row 19)
tests: 102 retrieval + evidence, exit 0
mutations: 2 attempted, 2 killed (one only after a survivor exposed an untested path)
remaining_open: ledger rows 13, 14, 16-18, 20-22, 25-34

checkpoint: V5.4-C3 — ledger correction, citation aggregation, final gates
status: see the remote verification recorded with this commit
local_head: this commit
changed_files: report only (no source change after C2)
tests: full suite 1559 passed / 3 skipped / 1562, 0 failed
remaining_open: ledger rows 13, 14, 16-18, 20-22, 25-34
```

## 31. V5.4 final verdict

**CERTIFIABLE WITH BOUNDED REMAINING SCOPE.**

What is certified by behaviour, not by a green suite:

* the review no longer claims truncation it did not perform (ledger row 12) — defect fixed, 4
  regression tests, 2 mutations killed;
* retrieval no longer invents a discovery ceiling from a length (ledger row 19) — defect fixed, 2
  regression tests at the real 4000/4001-row boundary, 2 mutations killed, one of them only after a
  surviving mutation exposed an untested path;
* the citation aggregation hypothesis was attacked and **disproved** with the exact `min`/`_RANK`
  evidence (ledger row 24), and the `NOT_CHECKABLE` contract is documented rather than silently
  collapsed (ledger row 23);
* the V5.3 "ten attacks / nine listed" inconsistency is resolved: nine was correct, and the true
  open set was 17 surfaces, now enumerated in a 34-row ledger.

What is **not** certified, and must not be read as certified — 13 ledger rows remain open:

`OPEN_NOT_EXERCISED`: 13 review null-well semantics · 14 review current/history matrix · 16 search
`truncated` semantics · 17 FTS/scan equivalence · 18 strict-query fallback metadata · 21 evidence
identity order-independence · 22 evidence freshness delta · 25 promotion atomicity · 26 child-row
identity · 28 defensive reads · 29 timeline interval semantics · 30 timeline malformed dates ·
31 timeline determinism · 33 performance at scale.

`OPEN_DEFECT`: none. The system-wide limit audit (row 34) is now written in
`docs/LIMIT_CONTRACTS.md` and pinned by `tests/integration/test_limit_contracts.py`; the false
cross-layer claim it uncovered is recorded in §26D.

Also recorded honestly: the MATCH + NOT_CHECKABLE citation combination is derived from `min` over
`_RANK` but has no test of its own (§26B).

## 32. V5.4 completion — checkpoints, gates and final ledger state

### 32.1 Checkpoints published this mission

```
checkpoint: V5.4-C4 — evidence package cap visibility
status: REMOTE_PUBLISHED
commit: fe102f8
push: exit 0, e2ab9d2..fe102f8
remote_verified: yes
changed_files: evidence/contract.py, evidence/service.py, cli/app.py,
               test_evidence_package_forensics.py, report
attacks_closed: ledger row 20
tests: 116 across evidence + retrieval + cli, exit 0
mutations: 3 attempted, 3 killed

checkpoint: V5.4-C5 — system-wide limit audit
status: REMOTE_PUBLISHED
commit: e740f52
push: exit 0, fe102f8..e740f52
remote_verified: yes
changed_files: evidence/contract.py, search/service.py, docs/LIMIT_CONTRACTS.md (new),
               tests/integration/test_limit_contracts.py (new), report
attacks_closed: ledger rows 34 (OPEN_DEFECT) and 32
tests: 5 new, exit 0
mutations: 2 attempted, 2 killed

checkpoint: V5.4-C6 — scope null hierarchy
status: REMOTE_PUBLISHED
commit: bbf9df7
push: exit 0, e740f52..bbf9df7
remote_verified: yes
changed_files: engineering/repository.py, test_calculation_forensics.py, report
attacks_closed: ledger row 27
tests: 6 new + 189 existing engineering, exit 0
mutations: 2 attempted, 2 killed

checkpoint: V5.4-C7 — final gates and verdict
status: REMOTE_PUBLISHED
commit: dcfff13
push: exit 0, bbf9df7..dcfff13
local_head: dcfff13b288aa9168362bb75abc463b6bf866051
remote_head: dcfff13b288aa9168362bb75abc463b6bf866051
remote_verified: yes - git ls-remote returned the same SHA as local HEAD
changed_files: report only (no source change after C6)
tests: full suite after the last source change, 1574 passed / 3 skipped / 1577, 0 failed
```

### 32.2 Final gate results

| gate | result |
| --- | --- |
| `ruff check src tests migrations` | exit 0, "All checks passed!" |
| `ruff format --check src tests migrations` | exit 0, 211 files already formatted |
| `python -m compileall -q src tests migrations` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` |
| migration suites (migrations + 0005 + 0011) | exit 0 |
| report integrity | exit 0, 5 passed |
| **full suite, after the last source/test change** | **1574 passed / 3 skipped / 1577, 0 failed, 0 errors** |

The full-suite total was derived by summing the per-character progress output with
`^([.sFEx]+)\s*\[\s*\d+%\]$`, because `pytest -q` again emitted no summary line;
`grep -cE '^FAILED'` returned 0. The total reconciles exactly with the V5.4-C3 figure of 1562 plus
the tests added since: 4 (evidence cap) + 5 (limit contracts) + 6 (scope hierarchy) = 1577.

No probe files remain: `tests/integration/test_zz_*` is empty.

### 32.3 Ledger movement across the mission

| verdict | at V5.4 start | at V5.4 end |
| --- | ---: | ---: |
| `DEFECT_FOUND_AND_FIXED` | 7 | **12** |
| `BEHAVIOURALLY_PROVEN` | 5 | **6** |
| `HYPOTHESIS_DISPROVED` | 1 | **2** |
| `OPEN_NOT_EXERCISED` | 20 | **13** |
| `OPEN_DEFECT` | 1 | **0** |

Five defects were found and fixed at the root, each with an executed mutation that restored the
defective behaviour and a test that failed because of it:

1. review claimed truncation it did not perform;
2. retrieval inferred a discovery ceiling from a length;
3. the evidence package discarded the ceiling its topics hit, in both JSON and CLI output;
4. a contract stated a falsehood about another layer's `limit=0` semantics;
5. a field and a project that contradicted each other were accepted as a scope.

### 32.4 Final verdict

**CERTIFIABLE WITH BOUNDED REMAINING SCOPE.**

Every `OPEN_DEFECT` is closed. Thirteen ledger rows remain `OPEN_NOT_EXERCISED`: 13 review null-well
semantics · 14 review current/history matrix · 16 search `truncated` semantics · 17 FTS/scan
equivalence · 18 strict-query fallback metadata · 21 evidence identity order-independence ·
22 evidence freshness delta · 25 promotion atomicity · 26 child-row identity · 28 defensive reads ·
29 timeline interval semantics · 30 timeline malformed dates · 31 timeline determinism ·
33 performance at scale.

These are unexamined, not cleared. The mission's own rule applies: an area is not complete because
existing tests are green, and none of these thirteen has been attacked with an adversarial fixture.

Two honesty notes carried forward rather than smoothed over:

* the `MATCH + NOT_CHECKABLE` citation combination is derived from `min` over `_RANK` but has no test
  of its own (§26B);
* one mutation attempt in Checkpoint 6 silently failed to apply and initially reported another
  mutation's failures. It was re-run with the mutation verified present in the file, and only that
  run is counted (§26E).

## 33. V5.4 close-out — checkpoints 8-10 and final state

### 33.1 Checkpoints

```
checkpoint: V5.4-C8 — supplied dates must not become "no date"
status: REMOTE_PUBLISHED
commit: d89365e
push: exit 0, 836566e..d89365e
remote_verified: yes
changed_files: operations/repository.py, test_field_intelligence.py,
               test_operation_dates.py (new), report
attacks_closed: ledger rows 29 (proven + documented), 30 (fixed); 31 recorded open with reason
tests: 4 new + 99 corpus/promotion/review, exit 0
mutations: 2 attempted, 2 killed

checkpoint: V5.4-C9 — promotion atomicity
status: REMOTE_PUBLISHED
commit: 38a1677
push: exit 0, d89365e..38a1677
remote_verified: yes
changed_files: test_promotion_atomicity.py (new), report
attacks_closed: none — row 25 left OPEN_NOT_EXERCISED with the reason recorded
tests: 3 new, pass on the real code
mutations: 2 attempted, 0 killed (both SURVIVED; one unexplained)

checkpoint: V5.4-C10 — final gates and verdict
status: see the remote verification recorded with this commit
changed_files: report only
tests: full suite after the last source/test change
```

### 33.2 Final gate results

| gate | result |
| --- | --- |
| `ruff check src tests migrations` | exit 0, "All checks passed!" |
| `ruff format --check src tests migrations` | exit 0 |
| `python -m compileall -q src tests migrations` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` |
| report integrity | exit 0, 5 passed |
| **full suite, after the last source/test change** | **1581 passed / 3 skipped / 1584, 0 failed, 0 errors** |

Derived by summing the per-character progress output with `^([.sFEx]+)\s*\[\s*\d+%\]$` because
`pytest -q` again printed no summary line; `grep -cE '^FAILED'` = 0. Reconciles exactly with the
previous 1577 plus 4 (operation dates) + 3 (promotion atomicity). No `tests/integration/test_zz_*`
probe files remain.

### 33.3 Final ledger movement

| verdict | V5.4 start | V5.4 end |
| --- | ---: | ---: |
| `DEFECT_FOUND_AND_FIXED` | 7 | **14** |
| `BEHAVIOURALLY_PROVEN` | 5 | **7** |
| `HYPOTHESIS_DISPROVED` | 1 | **2** |
| `OPEN_NOT_EXERCISED` | 20 | **11** |
| `OPEN_DEFECT` | 1 | **0** |

Seven defects found and fixed at the root, each with an executed mutation and a test that failed
because of it: review truncation truthfulness · retrieval cap inference · evidence package cap
visibility · a contract that lied about another layer's `limit=0` · field/project scope contradiction
· bare `date` dropped to NULL · unreadable dates laundered into "no date".

### 33.4 Final verdict

**CERTIFIABLE WITH BOUNDED REMAINING SCOPE.**

No `OPEN_DEFECT` remains. Eleven rows are still `OPEN_NOT_EXERCISED`, and they are unexamined rather
than cleared: 13 review null-well semantics · 14 review current/history matrix · 16 search `truncated`
semantics · 17 FTS/scan equivalence · 18 strict-query fallback metadata · 21 evidence identity
order-independence · 22 evidence freshness delta · 25 promotion atomicity · 26 child-row identity ·
28 defensive reads · 31 timeline determinism · 33 performance at scale.

Three honesty notes are carried in the report rather than smoothed over:

* **row 25** — three atomicity tests pass, but both mutations survived and one survivor is
  unexplained, so the tests are not proven sensitive (§26G);
* **row 31** — the comparator ties for two undated milestones of one well; the obvious tiebreaker
  sorts Completion before Spud, which is domain-wrong, so the change was reverted and the gap left
  recorded (§26F);
* the `MATCH + NOT_CHECKABLE` citation combination is derived from code but has no test (§26B).

## 34. V5.5 — fallback metadata (row 18) closed; rows 16 and 17 sharpened but still open

Starting point verified before any work: local `HEAD` had reverted to the grafted branch point
`e862113` while the remote held `648a73e`; `git diff --cached FETCH_HEAD --name-only` returned
**0 paths**, so the worktree was already byte-identical to the published tip and the recovery was a
fast-forward, not a restore. `.venv` had vanished and was re-provisioned.

### 34A. The contract, read out of the source

`SearchResponse.truncated` is **two facts OR'd together** (`search/index.py:736`):

* `scoring_truncated` — `len(hits) > MAX_CANDIDATES` *after* the filter set was applied (`:696`),
  i.e. the surviving result set was cut;
* `retrieval_truncated(mode)` — the backend's candidate discovery reached `RETRIEVAL_CAP`
  (`RETRIEVAL_CAP + 1` rows fetched, truncated when the `+1` came back), i.e. the candidate
  **universe** was bounded.

One boolean cannot say which happened. That is the honest statement of row 16 and it is now
written into `docs/LIMIT_CONTRACTS.md` rather than left as an inference. Discovery is ordered
`order by chunk_id` — **identity order, not relevance** — and the scope filter is applied *after*
the cap, in `_score`. Both facts are recorded; neither is exercised at the real constants yet.

### 34B. Row 18 — a real defect, found and fixed

`broadened` was `return self.mode == "any"`. That is an echo of the reading in use, not a report
of an event, so a caller who **asked** for the broadened reading was told a strict query had been
disproved when no strict query was ever attempted. Proven directly:

```
SearchResponse(query="x", mode="any").broadened  ->  True   # nothing was tested
```

The value propagates: `to_dict()` emits `"broadened"`, and `retrieval/service.py:296` feeds it into
`EvidenceBundle.discovery_broadened`, which evidence renders.

**Fix.** `score_candidates` now tracks a real `broadened` flag, set only inside the fallback branch,
and returns it in the metadata; `SearchResponse.broadened` is a reported field, not a derivation.
Both backends go through the one decision point (`index.py:873` and `:1258`), so a single change
covers SQLite and in-memory alike.

**Reach, stated plainly rather than inflated.** Neither the CLI nor `SearchService.search` exposes
the reading — `grep` finds no production caller passing `mode="any"`. So the false claim was
reachable at the index/API contract and in the JSON it emits, **not** through today's CLI, whose
sentence "no chunk matched every term" could in practice only fire after a genuine fallback. The
defect is real where the request is accepted; it is not a CLI regression, and it is not claimed as
one.

### 34C. Tests and mutations

Six tests, all against real SQLite, a real sidecar, FTS5 and a real ingestion run:

* `test_search_index.py::TestBroadenedMeansAFallbackHappened` (3) — an explicit broadened request
  reports no fallback; a genuine fallback still does; and a strict match **excluded by scope** does
  *not* broaden, preserving the existing distinction between "nothing matched" and "matched, but
  outside the scope you asked about".
* `test_search_pipeline.py::TestBroadenedWordingIsTruthful` (3) — the CLI sentence appears for a
  real fallback, is absent for a strict hit, and the API contract reports no disproof it never ran.

Mutations, each applied against a verified-unique anchor, grep-confirmed after applying, and
restored from a backup taken *after* the fix:

| mutation | result |
| --- | --- |
| MS1 — restore `"broadened": mode == "any"` | **KILLED** (1 failure) |
| MS2 — never set `broadened` on a real fallback | **KILLED** (1 failure) |

One harness error is recorded because it matters: the first restore used a backup taken **before**
the fix and silently reverted it. It was caught by grepping the restored file for the fix, not by
the test run, and the backup practice was corrected.

### 34D. Gates and ledger

ruff check / ruff format / `compileall -q` / `git diff --check` all exit 0; `alembic heads` =
`0012 (head)`; diff is 5 files, +116/−4. Full suite **1587 passed / 3 skipped / 1590, 0 failed**
(previously 1581 — exactly +6, reconciled test-by-test; the fourth `-k Broadened` match is the
pre-existing `test_the_broadened_fallback_is_reported_by_both`, which still passes).

| row | subject | status | note |
| --- | --- | --- | --- |
| 18 | Strict-query fallback metadata | `DEFECT_FOUND_AND_FIXED` | `broadened` echoed the mode instead of reporting an event; 6 tests, 2 mutations killed |
| 16 | Search `truncated` semantics | `OPEN_NOT_EXERCISED` | contract now documented as two conflated facts; not exercised at 3999/4000/4001 |
| 17 | FTS vs scan equivalence | `OPEN_NOT_EXERCISED` | not exercised at H1–H10 |

Rows 16 and 17 are **not** claimed. The boundary corpus they need (>16 000 indexed rows) was not
built in this pass, and no benchmark is asserted without having been run.

## 35. V5.6 — search truncation semantics and FTS/scan parity (rows 16 and 17)

**Starting state.** Local `HEAD` had again reverted to the grafted branch point `e862113` (shallow,
remote-tracking ref missing) while `git ls-remote` showed `e5e2603` — the expected baseline.
`git diff --cached FETCH_HEAD --name-only` returned **0 paths**, so the worktree was already
byte-identical to the published tip; recovery was a fast-forward, and `git diff --name-status
FETCH_HEAD` afterwards was empty with 0 dirty paths. `.venv` had vanished and was re-provisioned.
Nothing unpublished was lost.

### 35A. The contract, read out of the source

| item | value / semantics |
| --- | --- |
| `MAX_CANDIDATES` | `4000` (`index.py:91`). Applied in `_score` as `len(hits) > MAX_CANDIDATES` — **after** ranking and **after** the filter set, so it bounds the surviving result set. |
| `RETRIEVAL_CAP` | `16000` (`index.py:100`), applied **per population**: once to document chunks, once to structured records, OR'd into one flag. |
| discovery order | `ORDER BY chunk_id` on every path. `chunk_id` is `sha256("<version_id>:<index>")[:24]` (`chunking.py:283`) — identity order, unrelated to relevance *and* unrelated to insertion order, but computable in advance. |
| filter order | discovery → ranking → Python filters → `MAX_CANDIDATES` cut. Filters are deliberately last (see the `_score` comment): "no unit contains these words" and "the units that do belong to a scope you did not ask about" must stay distinguishable. |
| `truncated` | two facts OR'd (`index.py:736`): discovery hit `RETRIEVAL_CAP`, or surviving hits exceeded `MAX_CANDIDATES`. Still one boolean — see 35E. |
| `candidates` | rows **discovered** for the mode actually used, document + structured together, **before** filtering and before the cap cut. |
| FTS | `WHERE fts MATCH … ORDER BY chunk_id LIMIT RETRIEVAL_CAP + 1`; term-aware, so the bound applies to *matching* rows. |
| scan | **was** `ORDER BY chunk_id LIMIT RETRIEVAL_CAP` on the raw table; **now** the same term-aware shape via `instr(terms_json, '"<term>":')`. |
| in-memory | no discovery stage at all — examines every row, passes no `retrieval_truncated`. Can only ever report the `MAX_CANDIDATES` cut. |
| fallback | allowed only when `not hits and matched_any is False and mode == "all" and len(terms) > 1`. `matched_any` is computed **pre-filter**, so a scoped-out match never licences broadening. |

### 35B. Defect found: the scan bounded the table, not the candidates

**Symptom.** On a corpus of `RETRIEVAL_CAP + 1` chunks holding three genuine matches, one of whose
`chunk_id`s sorts past the bound, the same query through the same production stack answered
differently depending on whether SQLite had FTS5:

```
FTS5 : 3 hits, truncated=False, candidates=3
scan : 2 hits, truncated=True,  candidates=16000   <- the third match silently absent
```

**Root cause.** `_rows(None)` / `_structured_rows(None)` applied `LIMIT RETRIEVAL_CAP` to
`search_chunk` **before** any term matching, while `_candidate_ids` applies the same limit to rows
that already match. Capping the table first and matching afterwards is a different question.

**Why existing tests missed it.** Parity was only ever asserted on a small corpus, where both paths
fit entirely inside the bound and the asymmetry cannot express itself.

**Fix.** `_scan_candidate_ids` mirrors `_candidate_ids` exactly — same bound, same ordering, same
`+ 1` look-ahead — using the `instr(terms_json, '"<term>":')` predicate `_statistics` already uses,
so both paths read the row's own indexed vocabulary rather than its prose. It is wired in both where
FTS5 is absent and where an FTS expression is rejected. Semantic parity, bounded resources and
deterministic ordering all hold; no bound was removed and the scan is now strictly cheaper per
query (`candidates=3` instead of `16000` rows loaded into Python).

This also restores a promise the module already made in prose: *"a query that runs slower on a
machine without the extension must never be a query that returns a different list."*

**Post-fix measurement, same corpus and query:** both paths return the same three `chunk_id`s in the
same order with identical scores and matched terms, `truncated=False`, `candidates=3`.

### 35C. Tests added (all real SQLite, real sidecar, real FTS5, real constants)

Nine tests in `test_search_index.py`, over three module-scoped corpora built once each at the
**production** constants — not `monkeypatch(MAX_CANDIDATES=1)` over three rows:

* `late_match_index` — `RETRIEVAL_CAP + 1` chunks, three matches, one deliberately past the bound.
* `cut_index` — `MAX_CANDIDATES + 1` chunks that *all* match, so the surviving hits exceed the cap.
* `scoped_index` — `RETRIEVAL_CAP + 1` matching chunks, exactly one in scope, and that one is the
  row whose identity sorts last.

`TestRetrievalCapBoundaryIsAboutMatchingRows` (2) — FTS and scan return the same ids including the
late match, and the same ordering, scores and matched terms.
`TestMaxCandidatesBoundary` (2) — 4001 → cut to 4000 with `truncated=True`; then 4000 → **not**
truncated; then 3999 → not truncated. Exactly at the bound is distinguished from past it.
`TestScopeIsFilteredAfterDiscovery` (2) — State B: a bounded universe is reported even when few
results survive; and an in-scope row past the bound yields an empty answer that is **qualified** by
`truncated=True`, never an unqualified absence.
`TestBroadeningUnderBoundedDiscovery` (2) — a scoped-out strict match does not licence broadening;
a genuinely absent one still does, and only because its universe was fully examined.
`TestRetrievalCapExactBound` (1) — `RETRIEVAL_CAP` walked 16001 → 16000 → 15999 by removing
versions from the intact corpus. It uses the *scoped* query on purpose: an unscoped one leaves
16 000 surviving hits, which trips the result-set bound as well and would make the assertion about
the wrong fact.

Full-suite delta reconciles exactly: 1587 passed at V5.5 plus these 9 = **1596 passed / 3 skipped /
1599 collected, 0 failed** (see §35G for the executed run).

### 35D. Mutation matrix

Every mutation was applied against a verified-unique anchor, grep-read back after applying, restored
from a backup taken **after** the fix was known-good, and grep-verified on restore.

| mutation | result |
| --- | --- |
| M1 — `len(hits) > MAX_CANDIDATES` → `>=` | **KILLED** (2 failures) |
| M3 — scan reverts to `return None, False` (raw-table cap) | **KILLED** (2 failures) |
| M4 — `truncated` drops the discovery term | **KILLED** (2 failures) |
| M5 — `truncated` drops the result-set term | **KILLED** (2 failures) |

### 35E. What is *not* claimed

* **`truncated` is still one boolean for two facts.** A caller can infer which bound was reached
  (`candidates == RETRIEVAL_CAP` vs `candidates > MAX_CANDIDATES`) but that is an inference, not a
  field. Splitting it into explicit `candidate_capped` / `results_capped` metadata is the remaining
  work on row 16, and it is deliberately not done here: §18 forbids an API redesign for elegance, and
  the current field is backward-compatible and no longer *untruthful*.
* **Discovery is term-aware but not scope-aware.** An in-scope row past `RETRIEVAL_CAP` is genuinely
  not examined, so a scoped query can return nothing while a matching row exists. This is pinned by a
  test rather than fixed: `truncated=True` keeps it distinguishable from a proven absence, and making
  discovery scope-aware would push filters into SQL and duplicate the ranking module's semantics.
* **Broadening on an unproven negative is unreachable for plain-term queries** — `matched_any is
  False` means the strict expression matched no discovered row, and a discovery that matched nothing
  cannot itself have hit the bound. A phrase query at >16 000 scale (where FTS is a superset and the
  Python ranker may reject everything FTS returned) is the residual case; it is **not exercised**, so
  it is not claimed either way.
* **Row 33 (performance) is not certified.** Fixture build was measured at roughly 60 s per
  16 001-chunk corpus and the three corpora are built once per module; no benchmark claim is made.

### 35F. Ledger hygiene

§25 previously held **36 data rows with only 34 distinct numbers**: row 29 appeared twice with
*contradictory* statuses (`OPEN_NOT_EXERCISED` and a pass) and row 31 twice identically, so the open
count could not be derived mechanically. Both were reconciled to one row each — the later, more
precisely sourced wording kept, the superseded claim left on record in the section that made it.
§18, which reuses numbers 16/17/18 for *different* subjects, is now marked historical at the table
itself. Recount after the fix: **34 rows, 34 distinct numbers, no duplicates**; 12 fixed, 11 open,
9 behaviourally proven, 2 disproved.

### 35G. Executed gates

| gate | result |
| --- | --- |
| targeted: `test_search_index.py` boundary classes | 9 passed |
| targeted: search + retrieval + evidence + limit + CLI suites | 0 failed |
| **full suite (after the final change)** | **1596 passed / 3 skipped / 1599 collected, 0 failed**, exit 0 |
| `ruff check .` | all checks passed |
| `ruff format --check .` | all files formatted |
| `python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` |
| `tests/unit/test_report_integrity.py` | 5 passed |

The full-suite delta reconciles test by test: 1587 at V5.5 plus the 9 new boundary tests = 1596.

**Row 33 is not certified by this mission.** Fixture construction was measured at roughly 60 s per
16 001-chunk corpus (200 documents in 0.54 s, extrapolated and then confirmed by the actual runs);
the three corpora are module-scoped and built once each. No throughput or latency claim is made.
