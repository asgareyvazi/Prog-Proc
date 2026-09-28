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
| 16 | Search `truncated` semantics | query-honest, not corpus-size | `search/index.py:696` | `OPEN_NOT_EXERCISED` |
| 17 | FTS vs scan equivalence | the accelerator must not change the answer | `search/index.py` | `OPEN_NOT_EXERCISED` |
| 18 | Strict-query fallback metadata | broadening must describe the answer returned | `search/service.py` | `OPEN_NOT_EXERCISED` |
| 19 | Retrieval `capped` inference | `>=` cap does not prove truncation | `retrieval/service.py` `_discover` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 20 | Evidence package cap visibility | a package must not hide a discovery ceiling | `evidence/service.py` | `OPEN_NOT_EXERCISED` |
| 21 | Evidence identity order-independence | presentation must not change identity | `EvidenceQueryService._content_identity` | `OPEN_NOT_EXERCISED` |
| 22 | Evidence freshness delta | detect every mutation the contract promises | `check_freshness` | `OPEN_NOT_EXERCISED` |
| 23 | Citation `NOT_CHECKABLE` | "no citation" ≠ "citation failed" | `evidence/verify.py:59,116` | `BEHAVIOURALLY_PROVEN` (existing tests, see §26B) |
| 24 | Citation multi-aggregation | the worst citation decides the item | `evidence/verify.py:62,224` | `HYPOTHESIS_DISPROVED` (see §26B) |
| 25 | Promotion atomicity | late failure leaves no partial rows | `operations/promote.py` | `OPEN_NOT_EXERCISED` |
| 26 | Child-row identity | no inference from UUID or row position | `operations/*.py` | `OPEN_NOT_EXERCISED` |
| 27 | Scope null hierarchy | unknown ≠ contradictory ≠ unbound | `engineering/repository.py:273` | `OPEN_NOT_EXERCISED` |
| 28 | Defensive reads / corruption | no silent repair of malformed persisted data | read paths, `doctor` | `OPEN_NOT_EXERCISED` |
| 29 | Timeline interval semantics | interval vs point-in-window must be explicit | `intelligence/timeline.py` | `OPEN_NOT_EXERCISED` |
| 30 | Timeline malformed dates | no silent conversion to a clean result | `intelligence/timeline.py` | `OPEN_NOT_EXERCISED` |
| 31 | Timeline determinism | a total order on identical timestamps | `intelligence/timeline.py` | `OPEN_NOT_EXERCISED` |
| 32 | CLI limit contract | the CLI must not re-implement limit policy | `cli/app.py` | `OPEN_NOT_EXERCISED` |
| 33 | Performance at 1k / 10k | bounded, no N+1 | all read paths | `OPEN_NOT_EXERCISED` |
| 34 | System-wide limit audit | one matrix, five distinct bound kinds | 5 limit constants found | `OPEN_DEFECT` |

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

## 27. V5.4 mutation matrix

| mutation | target | tests failing | result |
| --- | --- | --- | --- |
| restore `size >= limit` in the truncation verdict | `review/service.py:1540` | 2 | **KILLED** |
| record `limit` instead of the query's real bound | `review/service.py` `note_bounded` | 2 | **KILLED** |
| infer `discovery_capped` from `length >= cap` | `retrieval/service.py` `_discover` | 1 | **KILLED** |
| discard `SearchResponse.truncated` | `retrieval/service.py` `_discover` | 1 | survived first run, **KILLED** after the gap was closed |

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

What is **not** certified, and must not be read as certified — 18 ledger rows remain open:

`OPEN_NOT_EXERCISED`: 13 review null-well semantics · 14 review current/history matrix · 16 search
`truncated` semantics · 17 FTS/scan equivalence · 18 strict-query fallback metadata · 20 evidence
package cap visibility · 21 evidence identity order-independence · 22 evidence freshness delta ·
25 promotion atomicity · 26 child-row identity · 27 scope null hierarchy · 28 defensive reads ·
29 timeline interval semantics · 30 timeline malformed dates · 31 timeline determinism ·
32 CLI limit contract · 33 performance at scale.

`OPEN_DEFECT`: 34 the system-wide limit audit. Five distinct limit kinds are in the tree
(`MAX_CANDIDATES`, `RETRIEVAL_CAP`, `_DISCOVERY_CAP`, `_SAFE_LIMIT`, `default_limit`) and have not
been reconciled into one matrix. Retrieval and review are now each internally honest; the
cross-layer contract is not yet written.

Also recorded honestly: the MATCH + NOT_CHECKABLE citation combination is derived from `min` over
`_RANK` but has no test of its own (§26B).
