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
| 21 | Evidence identity order-independence | presentation must not change identity | `evidence/service.py:122` | `BEHAVIOURALLY_PROVEN` — the real identity function was handed the same request, items and coverage in reversed and doubly-reversed order and returned the same address; a narrower scope returns a different one, so the equality is not vacuous |
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
| 12 | **Review truncation truthfulness** | a review must not claim a cut it did not make | `review/service.py:1540` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 13 | Review null-well semantics | null must never become an accidental wildcard | `review/service.py:466`, `engineering/repository.py:1081` | `BEHAVIOURALLY_PROVEN` — a record with no scope at all is fetched by nobody and appears in no well's review, while a positively field-scoped record is inherited by its wells; `_for_well`'s null allowance only ever sees rows a scoped query returned |
| 14 | Review current/history matrix | one definition of currentness per domain | `_current_for` is an explicit **table-keyed** matrix, not one rule: `program_target` inherits from the governing programme, `document_version` is the only table with `is_current`, `calculation` is historical by lineage *or* status, `knowledge_item` by SUPERSEDED/RETIRED, the operational tables only by REJECTED (a DRAFT report is still current), risk/recommendation by SUPERSEDED. Differences are intentional and now pinned per branch; M14-1/3/5/6 KILLED | `BEHAVIOURALLY_PROVEN` |
| 15 | Review read-only proof | a read must not write | `test_domain_review.py` fingerprint | `BEHAVIOURALLY_PROVEN` (existing) |
| 16 | Search `truncated` semantics | the two bounds must be separately knowable, and exact-bound must not read as exceeded | `search/index.py:696,736`, `search/service.py` | `DEFECT_FOUND_AND_FIXED` — `candidate_capped`/`results_capped` added, `truncated` kept as their OR; states A–E and both 3999/4000/4001 and 15999/16000/16001 walked at the real constants; serialisation proven; M1/M2/M4/M5/M6 killed (§35, §36A) |
| 17 | FTS vs scan equivalence | the accelerator must not change the answer | `search/index.py` | `DEFECT_FOUND_AND_FIXED` — two divergences found and fixed (scan bounded the table not the candidates, §35B; a shared bind name collapsed multi-term scans to nothing, §36B). Parity proven on ids, order, scores, matched terms, mode, broadened and both cap flags across documents, structured records, mixed populations, phrase and drilling tokens, at and below the bound, plus in-memory; M3/M8/M9 killed |
| 18 | Strict-query fallback metadata | broadening must describe the answer returned | `search/service.py` | `DEFECT_FOUND_AND_FIXED` |
| 19 | Retrieval `capped` inference | `>=` cap does not prove truncation | `retrieval/service.py` `_discover` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 20 | Evidence package cap visibility | a package must not hide a discovery ceiling | `evidence/contract.py:159`, `evidence/service.py:101` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 21 | Evidence identity order-independence | presentation must not change identity | `evidence/service.py:122` | `BEHAVIOURALLY_PROVEN` — the real identity function was handed the same request, items and coverage in reversed and doubly-reversed order and returned the same address; a narrower scope returns a different one, so the equality is not vacuous |
| 22 | Evidence freshness delta | detect every mutation the contract promises | `_content_identity` hashes the request, each item's (identity, source_type, record_type, status, current) and coverage - **not** wording, provenance or score, so the contract never promised content-level staleness; 6 tests pin both halves (fresh/added/removed/changed, order is not a change, an excluded edit is **not** claimed); `is_current=False` surfaces as `removed`, not `changed`; M22-3 + M22-5 KILLED | `BEHAVIOURALLY_PROVEN` |
| 23 | Citation `NOT_CHECKABLE` | "no citation" ≠ "citation failed" | `evidence/verify.py:59,116` | `BEHAVIOURALLY_PROVEN` (existing tests, see §26B) |
| 24 | Citation multi-aggregation | the worst citation decides the item | `evidence/verify.py:62,224` | `HYPOTHESIS_DISPROVED` (see §26B) |
| 25 | Promotion atomicity | late failure leaves no partial rows | **Boundary now measured, not assumed.** `promote.py` has 13 `flush()` and **zero `commit()`**; the only commits in `src` are the CLI, `unit_of_work` and document savepoints. New tests assert a successful promotion commits **exactly once**, a failed one commits **zero** times, and rollback is checked against a whole-row fingerprint rather than counts. M25-1 (writer commits midway) **KILLED** — the mutation that previously survived. M25-5 (drop the explicit `rollback()`) survived, and so did dropping `rollback()` *and* `close()` together: **proven semantically inert**, because atomicity rests on the absence of intermediate commits, so no partial state is ever visible whether or not rollback runs | `BEHAVIOURALLY_PROVEN` |
| 26 | Child-row identity | no inference from UUID or row position | `promotion_identity` is content-addressed over *what the source said **and where** it said it*, so `row_index` is in the key **by design**, and the `:op`/`:ev`/`:npt`/`:problem` suffixes keep one line's children apart. 6 tests pin it: re-promotion is a no-op, identical payload at different positions stays distinct, every component contributes, the key is deterministic and not a uuid. M26-1 (position only), M26-2 (drop `row_index`, collapsing by content), M26-3 (random uuid) all KILLED | `BEHAVIOURALLY_PROVEN` |
| 27 | Scope null hierarchy | unknown ≠ contradictory ≠ unbound | `engineering/repository.py:273` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 28 | Defensive reads / corruption | no silent repair of malformed persisted data | **DEFECT FOUND AND FIXED:** `NormalizedDocument.from_dict` defaulted every field, so a stored artefact missing its metadata came back as a *valid* empty document (blank sha256, zero pages) - the exact corrupt→valid-looking-default this row forbids - and the documented `extraction.unreadable` handler was dead code because `KeyError` never fired. Fixed at the authoritative layer by requiring the one identifying key. Also pinned from measurement: malformed provenance makes the review raise `JSONDecodeError` naming the position with the database byte-identical after; `StrEnumLike.parse` returns `None`, never a default member. M28-1/2/4 KILLED | `DEFECT_FOUND_AND_FIXED` |
| 29 | Timeline interval semantics | point-in-window over an index of dated facts, not a duration | `intelligence/timeline.py:326,341` | `BEHAVIOURALLY_PROVEN` — documented in §26F, boundary cases verified |
| 30 | Supplied dates must not become "no date" | a date given must be stored or refused | `operations/repository.py:112,133` | `DEFECT_FOUND_AND_FIXED` (V5.4) |
| 31 | Timeline determinism | a total order on identical timestamps | **DEFECT FOUND AND FIXED:** undated milestones of one well share kind, table and row_id and carry no instant, so they tied on *every* component of `entry_comparator` and the emitted order was merely append order; the same tie hits the dated side when spud and completion share a date. Fixed with an `ordinal` taken from the lifecycle definition (`_WELL_EVENTS`), not the title - alphabetically Completion precedes Spud, which is the wrong way round. 3 tests; M31-1 + M31-3 KILLED | `DEFECT_FOUND_AND_FIXED` |
| 32 | CLI limit contract | the CLI must not re-implement limit policy | `cli/app.py:663`, `docs/LIMIT_CONTRACTS.md` §3 | `BEHAVIOURALLY_PROVEN` for `--limit 0` and cap disclosure; per-command negative-value handling still unaudited |
| 33 | Performance at 1k / 10k | bounded, no N+1 | **Measured on real 1 000 and 10 000-row corpora** through the real service entry points. Review issues **39** SELECTs and timeline **9** at *both* scales - query count constant, no N+1. Bounds stay truthful at scale: at 10 000 the review is cut at `_SAFE_LIMIT` with `truncated=True`; search returns 20 of 10 000 candidates with `results_capped=True`, `candidate_capped=False`. Timings recorded, never asserted against an invented SLA (none exists in the repo). No search query count is published - the index is a separate SQLite file, so the instrumentation would report a misleading 0. M33-1 (per-row SELECT in `list_operations`) KILLED at both scales | `BEHAVIOURALLY_PROVEN` |
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

### 35H. Publication status — `PUBLISH_BLOCKED`

| item | value |
| --- | --- |
| starting SHA (verified baseline) | `e5e2603` |
| checkpoint 1 (production fix, isolation fixture, first 4 boundary tests) | `01a8600` — **pushed and `ls-remote`-verified during this session** |
| final commit (report, ledger reconciliation, docs, remaining 5 tests) | `b0b2219` — **local only** |
| worktree | 0 dirty paths |

After checkpoint 1 was published, the GitHub credential expired mid-session: `git push` returns
exit **128** (`could not read Username for 'https://github.com'`), and `git fetch` / `git ls-remote`
now fail the same way. `gh auth status` confirms it directly: *"The github.com token in `GH_TOKEN` is
no longer valid."* Three push attempts spaced 20 s apart all failed, so this is recorded as
`PUBLISH_BLOCKED` rather than reported as published.

**Nothing is lost.** `b0b2219` is a fast-forward child of the published `01a8600`, the worktree is
clean, and the branch needs reconnecting GitHub in Arena and then a single
`git push origin arena/01a0c936-prog-proc`. No `reset`, `rebase`, `squash` or force-push was used at
any point.

**Resolved later in the same session.** The credential recovered; `git push` then succeeded
(`01a8600..fd506b6`) and `git ls-remote` confirmed the remote tip equal to local `HEAD`, with 0
dirty paths. The block was transient, and no work was lost or rewritten while it lasted.

## 36. V5.6 continued — which bound was reached, and a defect the parity test caught

Published continuation from `fd506b6`. This section closes the two gaps §35E named for row 16 and
the structured/mixed gaps for row 17.

### 36A. `truncated` now names its own halves

`truncated` stays exactly the OR of two facts, for backward compatibility, but a caller no longer has
to infer which one fired:

| field | true means |
| --- | --- |
| `candidate_capped` | discovery hit `RETRIEVAL_CAP`, so the universe examined was **incomplete** — an absent row is not evidence of absence |
| `results_capped` | every discovered candidate was scored but more survived than `MAX_CANDIDATES` — the ranking is **complete**, only its tail was cut |
| `truncated` | `candidate_capped or results_capped`, unchanged in meaning |

Both are set in `score_candidates` (the one place both backends share), carried onto
`SearchResponse`, and emitted by `to_dict()`. The CLI now prints a different sentence for each, and
the old generic sentence is kept only as a fallback so an incomplete metadata set can never silently
drop the warning.

All five states are tested at the production constants — **A** neither bound, **B** candidate-bound
only, **C** result-bound only, **D** both, **E** exactly at a bound with nothing beyond it — plus a
parity assertion *at* the bound, where 16 001 rows match and the candidate list, count and both flags
are all products of the cap.

### 36B. A defect the parity test caught — in code this mission had just written

Adding structured/mixed parity over the real promotion path immediately failed. Measured:

```
Q=mud zermatt   FTS cands=56 hits=56     SCAN cands=0 hits=0
```

**Root cause.** `_scan_candidate_ids` built every term clause with the *same* bind parameter name
`:needle`. Combined under `or_()`, the clauses collapse onto the last term's value, so a two-term
broadened query silently became "the second term alone" — and since nothing matched it, the scan
returned **nothing at all** on a machine without FTS5 while FTS5 returned 56 rows. The `and_()` path
was equally wrong but happened to agree with FTS on every corpus tried so far, because both collapsed
to the same single term.

**Why it survived until now.** Every earlier parity test used a corpus where all the matching rows
contained *all* the query terms, so collapsing the AND onto one term selected the same rows. Only a
query whose terms do not co-occur exposes it.

**Fix.** One distinct bind name per term (`:needle0`, `:needle1`, …). Post-fix, all five real queries
agree exactly on candidates, hits, scores, matched terms, mode, broadened and both cap flags.

This is recorded as a defect **introduced and fixed inside this mission**, not as a pre-existing one.

### 36C. Tests and mutations added

* `TestTruncationStatesAreDistinguishable` (6) — states A–E plus parity at the bound.
* `TestDrillingTokensAndPhrasesAgreeAcrossBackends` (9 params) — `10.2`, `12 1/4`, `500/300`,
  `12 bbl`, `9,940 ft`, `10.2 ppg`, `shoe depth`, a quoted phrase, and a duplicated term.
* `TestInMemoryBackendSharesTheRankingContract` (2) — in-memory runs no discovery stage and must
  never claim a discovery bound; small-corpus parity with SQLite on ids, scores, mode and count.
* `TestTheCapContractSurvivesSerialisation` (3) — `to_dict()` carries both flags and the OR stays
  exactly the OR; the service re-derives nothing the backend did not report.
* `TestStructuredAndMixedParityAcrossBackends` (7) — structured and mixed document+structured
  queries through the real promotion path, FTS vs scan.

| mutation | result |
| --- | --- |
| M2 — drop the `+ 1` look-ahead from FTS discovery | **KILLED** |
| M6 — collapse `candidate_capped`/`results_capped` into `truncated` | **KILLED** (2) |
| M7 — drop `matched_any is False`, so a scoped-out match licences broadening | **KILLED** |
| M8 — give the scan a different cap policy (`RETRIEVAL_CAP // 2`) | **KILLED** |
| M9 — restore the shared `:needle` bind name | **KILLED** (3) |

M8 needed a test that did not exist: parity had only ever been asserted where both backends find all
the matches, which cannot detect a differing cap policy. `test_the_two_backends_agree_at_the_bound_itself`
was added for exactly that, and only then did M8 have something to kill.

### 36D. A test-isolation defect found by the full suite, and the executed gates

The first full-suite run after these additions failed one test:
`test_state_c_only_the_result_set_was_cut`. It passed in isolation and failed in the module, because
`TestMaxCandidatesBoundary` walks its shared `cut_index` down by removing versions — so "the result
set was cut" was no longer true by the time the state matrix ran. The state matrix now has its own
module-scoped corpus. The lesson recorded: **a boundary test that consumes its corpus must never
share a fixture with one that asserts an intact count**, and running a class in isolation proves
nothing about its position in the module. The whole module was then re-run in file order (75 tests,
0 failures) before the full suite.

| gate | result |
| --- | --- |
| targeted: `test_search_index.py` whole module, file order | 75 passed, 0 failed |
| targeted: structured + mixed parity | 7 passed |
| targeted: serialisation + CLI | 3 passed |
| **full suite (after the final change)** | **1623 passed / 3 skipped / 1626 collected, 0 failed**, exit 0 |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 234 files already formatted |
| `python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` |
| `tests/unit/test_report_integrity.py` | 5 passed |

Delta reconciles exactly: 1596 at the previous checkpoint plus 27 new test functions (6 state matrix
+ 9 drilling-token params + 2 in-memory + 3 serialisation + 7 structured/mixed) = **1623**.

**Rows 16 and 17 are now closed.** Open rows fall from 11 to **9**: 13, 14, 21, 22, 25, 26, 28, 31,
33. Row 33 in particular is still not certified — this section reports only that the boundary
fixtures build in roughly 60 s per 16 001-chunk corpus, which is a test-runtime observation, not a
performance certification.

### 36E. Publication status — blocked twice, then published

| item | value |
| --- | --- |
| last verified remote tip | `fd506b6` (pushed and `ls-remote`-verified earlier this session) |
| this section's commit | `799e89d` — **local only** |
| worktree | 0 dirty paths |

The GitHub credential expired a second time mid-session: `git push` returns exit **128** and
`gh auth status` reports *"The github.com token in `GH_TOKEN` is no longer valid."* Nine push
attempts spread over roughly seven minutes all failed, so this is recorded as `PUBLISH_BLOCKED`.
The identical block earlier in this session cleared on its own and the push then succeeded, which is
why this is labelled transient rather than final.

**Resolved.** The credential recovered again; `git push` then succeeded (`fd506b6..fd5fd5a`) and
`git ls-remote` confirmed the remote tip equal to local `HEAD` (`fd5fd5a`) with 0 dirty paths. Both
blocks were transient. Nothing was reset, rebased, squashed or force-pushed at any point, and no
work was lost while the blocks lasted.

## 37. Rows 13 and 21 closed from the repository, not from the docstring

### 37A. Row 13 — null scope is not a wildcard

`_for_well` (`review/service.py:466`) keeps rows with no `well_id`, and its docstring calls those
"genuinely well-wide rows". Read alone that looks like exactly the failure the standing prohibition
names — null scope becoming universal scope — so it was tested rather than argued about.

`DrillingProgram.well_id`, `field_id` and `project_id` are all nullable, and `_check_scope`
validates the *consistency* of a scope without requiring that at least one be present. So a record
with no scope at all is writable. It does not, however, reach any review: `programs_for_well` is
documented and implemented as "its own, then its field's and project's", so an unscoped record is
fetched by nobody. Measured on a real promoted workspace:

```
created unscoped program  well_id=None field_id=None project_id=None
  review A-3 : present = False
  review B-11: present = False
```

`_for_well`'s null allowance is therefore defensive: it only ever sees rows a *positively* scoped
query returned, which is what makes a field-wide record legitimately well-wide. The test pins both
halves of that contrast — the unscoped record appears in neither well, and the field-scoped one is
inherited — because the first half alone would also pass if inheritance were simply broken.

### 37B. Row 21 — identity is indifferent to presentation

`EvidencePackage.identity` is documented as excluding discovery rank and display order. The claim
was tested against the production identity function rather than believed: the same request, items
and coverage were handed back in reversed, coverage-reversed and doubly-reversed order and produced
the same address each time. A narrower scope produces a different address, so the equalities are not
vacuous.

Two details found while writing it, both recorded because they are the kind of thing that silently
makes such a test meaningless: `package.request` is the *serialised* request and carries a derived
`scope` key that is not a constructor argument, and a single-well query returns one item, where
"order" has nothing to be indifferent to.

### 37C. Ledger movement

Open rows fall from 9 to **7**: 14, 22, 25, 26, 28, 31, 33. The canonical table remains mechanically
explainable — 34 rows, 34 distinct numbers, no duplicates, statuses summing to 34 (14 fixed,
11 behaviourally proven, 7 open, 2 disproved).

Rows 25 and 31 stay open for the reasons already recorded and not repeated here: 25's two mutations
survived with one survivor unexplained, and 31's obvious tiebreaker sorts Completion before Spud,
which is domain-wrong. Row 33 stays open because no benchmark in this mission was run against the
repository's own performance-certification criteria.

### 37D. Executed gates for rows 13 and 21

| gate | result |
| --- | --- |
| targeted: `test_domain_review.py` | 13 passed, 0 failed |
| targeted: evidence package + citation forensics | 0 failed |
| **full suite (after the final change)** | **1625 passed / 3 skipped / 1628 collected, 0 failed**, exit 0 |
| `ruff check .` / `ruff format --check .` | all checks passed / 234 files already formatted |
| `python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` |
| `tests/unit/test_report_integrity.py` | 5 passed |

Delta reconciles exactly: 1623 at the previous checkpoint plus the 2 new tests (row 13, row 21) =
1625. Published as `019c3b3`, with `git ls-remote` equal to local `HEAD` and 0 dirty paths.

Neither row required a production change: both contracts already held, and what was missing was the
executed evidence. That is the distinction the ledger exists to keep — a row is not closed because
the code looks right, and it is not left open because nobody wrote the test down.

## 38. V5.7 Phase A — ledger rows 22 and 31

### 38A. Row 31 — timeline determinism — `DEFECT_FOUND_AND_FIXED`

**Symptom.** `entry_comparator` returned `(dated, timestamp, kind_rank, table, row_id)`. Well
milestones are emitted with `kind="well"`, `table="well"`, `row_id=well.id` — the `_kind` from
`_WELL_EVENTS` is discarded — so **Spud and Completion of one well are identical on every
component of the key** whenever they share an instant, and unconditionally when neither has one.
The emitted order was then merely the order the query appended them: reproducible on one machine,
but not a fact about the data, and reversing the input reversed the answer.

**Root cause.** The comparator is not *total*. Stability of Python's `sorted` made the output
deterministic for a fixed input order, which is exactly why no test caught it: every existing
assertion reads the list in the order the builder produced it.

**Fix.** `TimelineEntry.ordinal`, set from the position in the lifecycle definition
(`enumerate(_WELL_EVENTS)`), and used as the final component of both comparator branches. The
tie breaker comes from the *domain*, not the title: alphabetically "Completion" precedes "Spud",
which is the wrong way round for a well's life. That is precisely why the earlier title-based
attempt was reverted.

**Tests** (`tests/integration/test_field_intelligence.py`): undated milestones follow the
lifecycle and reversing the input cannot change the answer; **same-day** milestones do too (the
dated branch needs the ordinal for the same reason); the ordinal survives `to_dict()`.

**Mutations.** `M31-1` (drop the ordinal from the dated branch) and `M31-3` (drop it from the
undated branch) both **KILLED**. Honest note: `M31-1` **first survived**, for two compounding
reasons — the mutation harness's `-k` filter did not yet name the new test, and the dated branch is
never reached when both dates are `NULL`. Adding the same-day case and widening the filter killed
it. A survival is a finding, not a pass.

### 38B. Row 22 — evidence freshness delta — `BEHAVIOURALLY_PROVEN`

**The row's premise was wrong, and reading the source settles it.** `_content_identity` hashes the
request, each item's `(identity, source_type, record_type, status, current)` and the coverage row —
**not** the wording, provenance or score. So the contract never promised content-level staleness,
and "identical identities hide a substantive change" is a *true statement about a contract that
does not claim otherwise*. The honest closure is to prove both halves rather than invent a change.

**Tests** (`tests/integration/test_evidence_package_forensics.py`): unchanged is fresh with an
empty delta and matching identities; a new answer is named in `added`; a deleted answer is named
in `removed`; a status change on a row that still answers is reported as `changed` **with from/to**
— the class a set-diff implementation cannot see at all; presentation order is *not* a freshness
change, so row 21's guarantee is not silently undone here; and a substantive text edit the
contract excludes is pinned as **not** reported, so a future identity change cannot start firing on
edits nobody asked it to track.

**One honest finding.** `is_current=False` removes the row from the answer entirely, so it surfaces
as `removed`, not `changed`. Which class a mutation lands in is a fact about the *retrieval*
contract, not about the freshness comparison — the first version of this test assumed otherwise and
was corrected against the measurement.

**Mutations.** `M22-3` (ignore status/current) and `M22-5` (make ordering affect freshness) both
**KILLED**. `M22-3` also killed a pre-existing status test, so this was a gap in the delta-class
*matrix* rather than a wholly unguarded path.

### 38C. Ledger

34 rows, 34 distinct numbers, no duplicates; `BEHAVIOURALLY_PROVEN 12`, `DEFECT_FOUND_AND_FIXED 15`,
`HYPOTHESIS_DISPROVED 2`, `OPEN_NOT_EXERCISED 5` = 34. Row 12's status cell was bold-wrapped
(`**`…`**`), the only non-canonical form in the table; normalised without dropping its `(V5.4)`
mission tag. **Open rows are now 14, 25, 26, 28, 33** — reduced from seven, by closing rows 22 and
31, with no row deleted and no historical evidence removed.

## 39. V5.7 Phase A — final report

**STARTING STATE:** expected baseline `180392c5c8aa84daa6218e587968019f98ebb64a` (= last published
commit of the V5.6 series). The Arena workspace had reverted again: local `HEAD` was the grafted
`e8621136ca73108ae7b590e6baa72fedc1f00835`, `is-shallow-repository = true`, 102 dirty paths, and
`.venv` was absent. `git ls-remote origin refs/heads/arena/01a0c936-prog-proc` returned
`180392c…`, the expected baseline.

**DIVERGENCE / RECOVERY.** The 102 dirty paths were inspected *before* any checkout: after
`git add -A`, `git diff --cached FETCH_HEAD --name-only | wc -l` returned **0**, so the worktree
already equalled the remote tip and recovery was byte-identical and lossless.
`merge-base --is-ancestor e862113 FETCH_HEAD` held, so `git checkout FETCH_HEAD -- .` followed by
`git merge --ff-only FETCH_HEAD` fast-forwarded to `180392c`. Verified: local == remote ==
`180392c`, `diff_vs_tip = 0`, `dirty = 0`. `.venv` was re-provisioned (`pip install -e ".[dev]"`),
`alembic heads` → `0012 (head)`. **No reset, rebase, squash or force-push was used.**

**FILES CHANGED:** `src/drilling_intelligence/intelligence/timeline.py` (`TimelineEntry.ordinal`,
`_entry(ordinal=)`, `entry_comparator` sixth component, `_WELL_EVENTS` enumeration),
`tests/integration/test_field_intelligence.py` (+3), `tests/integration/test_evidence_package_forensics.py` (+6),
this document.

**SCHEMA / MIGRATION CHANGES:** none. `ordinal` is a dataclass field with a default, derived at
read time from the lifecycle definition; it is not persisted. `alembic heads` is still `0012`.

**DEFECTS FOUND (2).**

1. **Timeline comparator is not total** (`intelligence/timeline.py`). *Symptom:* Spud and
   Completion of one well carry `kind="well"`, `table="well"`, `row_id=well.id` and, when undated
   or same-dated, no distinguishing instant — so they tied on every component of
   `entry_comparator`. *Root cause:* the key has no domain ordinal; the `_kind` from
   `_WELL_EVENTS` is discarded. *Layer:* `intelligence`. *Why tests missed it:* `sorted` is stable,
   so a fixed builder order gives a fixed answer and every existing assertion reads the list in the
   order the builder produced it. *Regression:* 3 tests. *Mutations:* `M31-1`, `M31-3` **KILLED**.

2. **Row 22's premise was incorrect** (`evidence/service.py`). *Symptom (claimed):* identical
   item identities hide a substantive change. *Actual:* `_content_identity` hashes request +
   `(identity, source_type, record_type, status, current)` + coverage — never wording, provenance
   or score — so content-level staleness was never promised. *Layer:* contract, not code. *Why
   tests missed it:* no test pinned the *excluded* direction, so nothing recorded the boundary.
   *Regression:* 6 tests covering both halves. *Mutations:* `M22-3`, `M22-5` **KILLED**.

**HONEST FINDINGS / LIMITATIONS.** (a) `M31-1` **survived on its first run**: the harness's `-k`
filter did not name the new test, and the dated comparator branch is unreachable when both dates
are `NULL`. The same-day test plus a wider filter killed it. (b) `is_current=False` removes a
lesson from the answer entirely, so it surfaces as `removed`, not `changed`; the first draft of
that test assumed `changed` and was corrected against the measurement. (c) `M22-3` also killed a
pre-existing status test, so row 22 was a gap in the delta-class matrix rather than an unguarded
path. (d) A regression command naming a non-existent `tests/integration/test_evidence_service.py`
exited 4 and ran nothing; it was caught, the correct files were located
(`test_evidence_package_forensics.py`, `test_evidence_citation_forensics.py`) and re-run — 92
tests, 0 failures — against the already-pushed commit.

**CONTRACT CHANGES:** `entry_comparator`'s key gained a sixth component (`ordinal`) and
`TimelineEntry` a field with a default. No public signature lost an argument; `to_dict()` gained a
key, which is additive.

**VERIFICATION EXECUTED:** `pytest tests/integration/test_field_intelligence.py` (30 tests, incl.
the 3 new), `pytest tests/integration/test_evidence_package_forensics.py
tests/integration/test_evidence_citation_forensics.py` (57, 0 failures), `test_report_integrity.py`
(5 passed), `ruff check .` clean, `ruff format --check .` clean, `python -m compileall` exit 0,
`git diff --check` exit 0. Mutations: **5 run, 5 KILLED** (`M31-1`, `M31-3`, `M22-3`, `M22-5` plus
the corrected re-run of `M31-1`), every mutation read back after application and every restore
verified byte-identical before recording.

**OPEN LEDGER ROWS AFTER THIS MISSION:** **14** (review current/history matrix), **25** (promotion
atomicity), **26** (child-row identity), **28** (defensive reads / corruption), **33** (performance
at 1k/10k). **Not closed in this session, and not claimed:** rows 14, 25, 26, 28 and 33 were not
reached — Phase B (25/26), Phase C (28) and Phase D (33) remain outstanding. Row 33 in particular
requires an executed 1k/10k benchmark against the stated performance criteria and has had none.

**PUBLICATION:** three commits pushed to `arena/01a0c936-prog-proc` — `f00f8c4` (row 31),
`ccc7d84` (row 22), and this ledger/report commit. After each push,
`git rev-parse HEAD` == `git ls-remote origin refs/heads/arena/01a0c936-prog-proc` and
`git status --porcelain` was empty. No force-push, rebase or amend.

**FULL SUITE (run after the last source change).** `SUITE_EXIT=0`. Counted mechanically from the
progress characters (23 progress lines, percentage markers stripped): **1634 passed, 3 skipped,
1637 total, 0 failed, 0 errors**. Baseline was 1625 passed / 3 skipped / 1628, so the delta is
**exactly +9 passed, +0 skipped** = the 3 row-31 tests plus the 6 row-22 tests, with no test
removed, renamed or silently disabled.

**GATE TABLE**

| Gate | Result |
| --- | --- |
| Full suite (`pytest -q`) | 1634 passed / 3 skipped / 1637, 0 failed, exit 0 |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 234 files already formatted |
| `python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` — no new migration |
| `test_report_integrity.py` | 5 passed |
| Mutations | 5 run, 5 KILLED, all restores verified byte-identical |
| `local == origin == ls-remote`, 0 dirty | verified after every push |

## 40. V5.8 — final zero-open certification closure

### 40A. Starting state and recovery

Expected baseline `57c974a81ad52f98b24989ce89f51571b0199bd4`. The workspace had reverted again:
local `HEAD` was the grafted `e8621136ca73108ae7b590e6baa72fedc1f00835`, `is-shallow-repository =
true`, **103 dirty paths**, `.venv` absent. `ls-remote` returned the expected `57c974a`.

The 103 dirty paths were inspected *before* any checkout: `git add -A` then
`git diff --cached FETCH_HEAD --name-only | wc -l` returned **0**, so the worktree already equalled
the remote tip and recovery was byte-identical. `merge-base --is-ancestor e862113 FETCH_HEAD` held;
`git checkout FETCH_HEAD -- .` then `git merge --ff-only FETCH_HEAD` fast-forwarded to `57c974a`.
Post-recovery: local == `ls-remote` == `57c974a`, `diff_vs_tip = 0`, `dirty = 0`. `.venv`
re-provisioned; `alembic heads` → `0012 (head)`. **No reset, rebase, squash or force-push.**

### 40B. Row 14 — review current/history — `BEHAVIOURALLY_PROVEN`

The lifecycle model was already explicit; what was missing was a proof that its per-domain
differences are deliberate. `_current_for` is a table-keyed matrix: `program_target` inherits
currentness from its governing programme; `document_version` is the **only** table with an
`is_current` column; `calculation` is historical by lineage *or* by its own `SUPERSEDED` status;
`knowledge_item` by `SUPERSEDED`/`RETIRED`; the operational tables only by `REJECTED`, so a `DRAFT`
daily report is still current; `risk_record`/`recommendation` by `SUPERSEDED`. Flattening these
would be wrong in both directions, so the new test pins each branch on real model instances and
asserts *why* the answer differs. The service-level gate was already pinned by the existing
programme/target history test. **M14-1, M14-3, M14-5, M14-6 KILLED.**

### 40C. Row 25 — promotion atomicity — `BEHAVIOURALLY_PROVEN`

**Transaction ownership, measured:** `operations/promote.py` contains 13 `flush()` calls and **zero
`commit()`**. The only commits anywhere in `src` are the CLI layer, `Database.unit_of_work`, and
three `savepoint.commit()` calls in `documents/repository.py` (nested savepoints, not real
commits). So the caller-owns-the-transaction architecture is now a measurement, not a reading.

The previous tests compared **row counts**, which is exactly why a mid-commit mutation survived.
Three new tests instrument the boundary itself: a successful promotion commits **exactly once**; a
failed promotion commits **zero** times; and the rollback is asserted against a whole-row
fingerprint (identities, foreign keys, payloads), not counts. **M25-1 KILLED** — the mutation that
previously survived.

**Honest inertness result.** M25-5 (delete the explicit `session.rollback()`) **survived**, and so
did removing `rollback()` *and* `close()` together. That is the architecture, not a test gap:
because promotion never commits, no partial state is ever visible to another connection whether or
not rollback runs. The explicit rollback is prompt resource release, not the guarantee. Recorded as
proven semantically inert rather than forced into a false kill.

### 40D. Row 26 — child identity — `BEHAVIOURALLY_PROVEN`

`promotion_identity` is content-addressed over *what the source said **and where** it said it*, so
`row_index` is part of the key **by design** — the docstring says an extraction that moved a value
must produce a different key. Children of one line are separated by the `:op` / `:ev` / `:npt` /
`:problem` suffixes. Six tests pin the consequences: re-promotion is a no-op; identical payload at
different positions stays **distinct** (collapsing them would silently lose a row); every component
contributes, so none is decorative; the key is deterministic and not a uuid. **M26-1, M26-2, M26-3
KILLED.** End-to-end re-promotion idempotence was already covered by
`test_promoting_the_whole_workspace_twice_is_stable` and
`test_records_promote_is_idempotent_from_the_terminal`, so it is cited rather than duplicated.

### 40E. Row 28 — defensive reads / corruption — `DEFECT_FOUND_AND_FIXED`

**A real production defect.** `NormalizedDocument.from_dict` defaulted every field, so a stored
artefact missing its metadata entirely came back as a **valid-looking empty document** (blank
`sha256`, `page_count=0`, no diagnostics) instead of as the corruption it is — precisely the
corrupt → valid-looking-default conversion this row forbids, on a live read path. It also made
`DocumentRegistry.extraction_document`'s `except (JSONDecodeError, KeyError)` handler **dead code**,
marked `# pragma: no cover - corrupt store`, because `KeyError` could never fire.

**Fix at the authoritative layer:** `from_dict` now requires the one key that identifies the
document, raising `KeyError` so the existing documented handler fires and logs
`extraction.unreadable`. No new abstraction and no new error type — the project's own vocabulary
simply becomes live. The remaining defaults stay, because an extraction that genuinely found no
tables is not corrupt. 216 extraction/document/registry tests still pass.

Also pinned **from measurement rather than assumption**: malformed provenance JSON makes the review
read raise `json.JSONDecodeError` naming the parse position, with the database byte-identical
afterwards (a read that repairs would be a write in disguise); `StrEnumLike.parse` answers `None`
for a value the enum does not have, never a default member, and does not silently lowercase; and
changing a row's recorded provenance changes the review answer. **M28-1, M28-2, M28-4 KILLED.**

### 40F. Row 33 — performance at 1k / 10k — `BEHAVIOURALLY_PROVEN`

The repository defines **no numeric latency SLA**, so the criterion is structural and the test
asserts only what was measured. Timings are recorded, never compared against an invented threshold.

| surface | workload | rows | queries | returned | runtime (s) | bound state |
| --- | --- | --- | --- | --- | --- | --- |
| `DomainReviewService.review` | 1 000 | 2 000 | **39** | 2 000 | 0.220 | `truncated=False` |
| `DomainReviewService.review` | 10 000 | 20 000 | **39** | 10 000 | 1.961 | `truncated=True` (`_SAFE_LIMIT`) |
| `build_timeline` | 1 000 | 2 002 | **9** | 2 002 | 0.022 | — |
| `build_timeline` | 10 000 | 20 002 | **9** | 20 002 | 0.240 | — |
| `SearchService.search` | 1 000 | 1 000 | n/p | 20 | 0.035 | no cap |
| `SearchService.search` | 10 000 | 10 000 | n/p | 20 | 0.312 | `results_capped=True`, `candidate_capped=False` |

**Certification finding: query count is constant — 39 for review, 9 for timeline — at both scales.**
That is the N+1 criterion satisfied by measurement, not by inspection. Bounds remain truthful at
scale rather than being bypassed for speed: the 10 000-row review is cut at `_SAFE_LIMIT` and says
so; search reports exactly the state-C distinction (`results_capped` without `candidate_capped`)
established by the truncation work.

**Two measurement-honesty notes.** (1) `WellOperation` is *not* part of the search index's
structured population — `WellEvent`, `NptRecord`, `ProblemOccurrence` and `LessonLearned` are — so
the corpus carries both; measuring search against operations alone returned 0 results and would
have certified nothing. (2) There is deliberately **no search query count**: the index is its own
SQLite file with its own engine, so instrumenting `workspace.database.engine` reported a misleading
`0`. That number is omitted rather than published as a result. **M33-1** (replace the single ordered
read in `list_operations` with a per-row `SELECT`) **KILLED at both scales.**

### 40G. Cross-row sweep and recent-closed-row smoke

Row 14's matrix change touched no identity code, so row 22's evidence identity contract is
unaffected; row 25's fingerprint test asserts whole-row identity, so a failed replace cannot leave
reassigned children (rows 25 × 26); row 28's corruption tests assert the database is byte-identical
after each read, so a corrupt row cannot quietly alter a package's freshness (rows 28 × 22); row 31's
ordinal is domain-derived and independent of review retrieval order (rows 31 × 14); and row 33's
benchmark asserts the *caps still fire* at 10 000 rows, so no optimisation bypassed the semantic
contracts (row 33 × the rest). Rows 13, 16, 17, 18, 21, 22 and 31 were re-run in the final full
suite with no failures and none is reopened.

### 40H. Mutations run this mission

**16 mutations, 15 KILLED, 1 proven semantically inert, 0 unexplained survivors.**

| Mutation | Target | Result |
| --- | --- | --- |
| M14-1 | target currentness always true | KILLED |
| M14-3 | superseded calculation admitted as current | KILLED |
| M14-5 | field inheritance dropped, well-local kept | KILLED |
| M14-6 | history silently behaves like current | KILLED |
| M25-1 | repository writer commits mid-promotion | KILLED (previously survived) |
| M25-5 | explicit `rollback()` removed | survived — **proven inert** |
| M25-5b | `rollback()` *and* `close()` removed | survived — **proven inert** |
| M26-1 | identity from position only | KILLED |
| M26-2 | `row_index` dropped, children collapse by content | KILLED |
| M26-3 | identity from a random uuid | KILLED |
| M28-1 | metadata guard removed | KILLED |
| M28-2 | invalid enum coerced to first member | KILLED |
| M28-4 | handler returns an empty document, not "unreadable" | KILLED |
| M33-1 | per-row `SELECT` in `list_operations` | KILLED at 1k and 10k |

Every mutation was applied to a verified-unique anchor, read back after application, and every
restore verified byte-identical before its result was recorded. Backups were taken **after** each
fix.

**Two harness faults found and corrected, both recorded because each nearly produced false
evidence.** (1) M28-4 was first reported `SURVIVED`; the mutation was sound but my `sed` had pointed
the runner at `test_corrupt_persistence.py` while the killing test lives in
`test_extraction_cache.py`. Re-run against the correct file it is KILLED. (2) An earlier row-31
survival had the same cause. A mutation wrongly called *survived* is as misleading as one wrongly
called killed.

### 40I. Final gates

| Gate | Result |
| --- | --- |
| `pytest -q` (full, authoritative) | **1651 passed / 3 skipped / 1654, 0 failed, exit 0** |
| Test delta vs the 1634/3/1637 baseline | **+17 passed, +0 skipped** — exactly the 17 tests added |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 236 files already formatted |
| `python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |
| `alembic heads` | `0012 (head)` — no migration added |
| `test_report_integrity.py` | 5 passed |
| Recent-closed-row smoke (13, 16, 17, 18, 21, 22, 31) | 97 tests, 0 failed |
| Mutations | 16 run, 15 KILLED, 1 proven inert, 0 unexplained survivors |

The delta reconciles exactly: 1 (row 14) + 3 (row 25) + 6 (row 26) + 4 (row 28 corruption matrix) +
1 (row 28 extraction cache) + 2 (row 33, parametrised 1k/10k) = **17**.

One process fault, recorded rather than hidden: the first smoke-suite command named
`tests/integration/test_search_truncation_boundary.py`, which does not exist, so pytest exited 4 and
ran nothing. The real files were located (`test_search_index.py`, `test_search_pipeline.py`,
`test_search_structured_forensics.py`, `test_retrieval_forensics.py`) and the suite re-run — 97
tests, 0 failures. This is the same mistake made in the previous mission, so the lesson is now
written down: verify a path exists before citing a result from it.

### 40J. Final ledger

34 rows, 34 distinct numbers, no duplicates. **`BEHAVIOURALLY_PROVEN` 16, `DEFECT_FOUND_AND_FIXED`
16, `HYPOTHESIS_DISPROVED` 2, `OPEN_NOT_EXERCISED` 0** — summing to 34, counted by splitting the
table's cells rather than by reading it. No row was deleted and no historical evidence removed.

## 41. V6.0 — post-certification production readiness

This section is a **release-readiness** audit, not a reopening of the 34-row certification ledger,
which stays resolved and untouched.

### 41A. Starting state and recovery

Expected baseline `cb0ffb8e2ef5eecc0f424d5424184a60a8903a26`. The workspace had reverted again:
grafted `e862113`, `shallow=true`, **107 dirty paths**, `.venv` absent; `ls-remote` returned the
expected `cb0ffb8`. Inspected before touching anything — `git add -A` then
`git diff --cached FETCH_HEAD --name-only | wc -l` returned **0**, so the worktree already equalled
the remote tip and recovery was byte-identical. Fast-forwarded to `cb0ffb8`; post-recovery
local == `ls-remote`, `diff_vs_tip = 0`, `dirty = 0`. **No reset, rebase or force-push.**

### 41B. Release-readiness matrix

| Area | Finding | Severity | Evidence | Fix | Status |
| --- | --- | --- | --- | --- | --- |
| CLI global options | `drillintel --workspace W wells list` ignored `W` and ran against the **cwd**; `--json`/`--debug`/`--config` in that position were dropped too | **High** — a documented flag silently did nothing, and machine output came back as prose | argparse applies a subparser's defaults over the namespace the parent filled | `SUPPRESS` defaults in `_common()`, real defaults on the top-level parser, which needs its *own* `_common()` instance because `set_defaults` rewrites `action.default` on shared action objects | **FIXED**, 6 tests, M60-1 + M60-2b KILLED |
| `doctor` on an installed package | Reported `schema is at '0012' while head is ''` and **exited 1** on a healthy wheel-installed workspace, advising `alembic upgrade head` — impossible without the scripts the wheel does not ship | **High** — a false finding that also fails the exit code trains operators to ignore doctor | `upgrade()`'s migrations-unavailable branch returned `head=""`; `up_to_date` requires a non-empty head | The head an installed package can attest to is `METADATA_REVISION`, which is what its schema was built from | **FIXED**, 1 test, M60-3 KILLED, verified from a rebuilt wheel *and* sdist |
| CI | **None existed.** `docs/DECISIONS.md` ADR-0002 says "two runtimes are exercised in CI" with nothing behind it | **High** — the compatibility claim was unenforceable | no `.github` directory | `.github/workflows/ci.yml`: CPython 3.11 + 3.14, ruff check/format, compileall over src+tests+migrations, single-alembic-head gate, full pytest, plus a clean-install smoke job | **FIXED**, and observed queued→running on GitHub |
| README quick start | `PYTHONPATH=src` (unnecessary — the editable install resolves from anywhere) and a frozen "1083 passed … V2 certification run" count | Medium — misleading, and hides the sys.path parity problem | verified by importing from `/tmp` and running a test module with `PYTHONPATH` unset | Command stands alone; the count now lives only in dated certification records where it is a historical fact; CI and the smoke are documented | **FIXED** |
| Migration discovery from a wheel | Suspected release defect; **not a defect** | — | A clean wheel install creates a workspace and stamps `0012` via `stamped-from-metadata` (45 tables, `alembic_version` present) | none needed — the documented bootstrap works | **VERIFIED SOUND** |
| Path / symlink boundary | Suspected traversal; **not a defect** | — | A scan root containing a symlink to an outside *file* and to an outside *directory* registered **1** file — only the real one | none needed | **VERIFIED SOUND** |
| Optional-dependency boundary | Vector and Qt extras are genuinely optional | — | The clean smoke venv installs neither and the package imports, the CLI runs and every read surface works | none needed | **VERIFIED SOUND** |
| Version `0.0.1a0` | Consistent with `Development Status :: 2 - Pre-Alpha` and `requires-python >=3.11` | — | `pyproject.toml` classifiers agree with the metadata | left as-is deliberately | **INTENTIONAL** |
| Historical test counts | Several dated reports quote old counts (1077, 1089, 1291, 1313) | — | they are historical records of what was true then | **not** changed — rewriting history is out of scope | **INTENTIONAL** |

### 41C. Packaging, verified by execution not inspection

`tools/release_smoke.py` is the executable form of the installation question, and it is what CI's
second job runs. For **each** of the wheel and the sdist it: builds through the project's own
backend; installs into a fresh virtualenv outside the checkout; asserts the package imports from
`site-packages` with **no** repository path on `sys.path`; runs `drillintel --help`/`--version`;
creates a workspace; opens it (which bootstraps the schema from ORM metadata and stamps `0012`, the
only path a wheel has); creates a well and reads it back; exercises `records list`/`summary`/`review`,
`search`, `timeline`, `knowledge status`, `index status`; and runs `doctor` including its `--json`
shape. Locally: **all checks pass for both artefacts.** The wheel carries 119 entries and **zero**
Alembic revision scripts, which is intentional and now matched by `doctor` treating that mode as
healthy rather than as drift.

Two of my own smoke-script bugs were found and fixed while writing it — `records` needs a subaction
and a scope — and the CLI's refusals were correct and well-hinted in both cases. Those were script
errors, not product defects, and are recorded as such.

### 41D. Mutations this mission

| Mutation | Target | Result |
| --- | --- | --- |
| M60-1 | drop `SUPPRESS` from `--workspace` | **KILLED** |
| M60-2b | share one `_common()` instance between top level and subparsers (the shape of the failed first attempt) | **KILLED** |
| M60-3 | `head=METADATA_REVISION` back to `head=""` | **KILLED** |
| M60-2 | rename to an undefined symbol | **INVALID MUTATION** — it raised `NameError`, so all six tests failed for the wrong reason and it proves nothing |

One honest process note: the **first attempt at the CLI fix did not work**. `set_defaults()` does not
merely record a namespace default — it rewrites `action.default` on any matching action object, and
`parents=` shares those very objects between parsers, so the real defaults leaked into the
subparsers and undid the `SUPPRESS`. The failure was found by parsing argv directly and inspecting
`action.default`, not by re-running the CLI and hoping.

### 41E. What CI actually found — three real gaps, in order

Adding CI was not a formality; it immediately found three things that no local run had ever caught,
each of them a genuine release-readiness defect rather than a style complaint.

**1. The suite was only runnable one specific way.** `tests/conftest.py` imports
`tests.fixtures.generate`, so the repository root must be importable. `python -m pytest` puts the
current directory on `sys.path` and hides that; the bare `pytest` console script does not, and died
in under a second with `ModuleNotFoundError: No module named 'tests'` — exit 4, before a single test
ran. Every local invocation in this project's history used the `python -m` form, so nothing had ever
exercised the documented command. Fixed at the root with `pythonpath = ["."]` in the pytest config
rather than by changing CI to the `python -m` form, so both invocations are equivalent and neither
depends on how it was typed. Verified from the repository root *and* from a subdirectory.

**2. The certification reports could not be validated in a normal clone.**
`test_report_integrity` asserts that every full-length commit SHA cited in the reports exists in the
object database — a real guard against a report naming a commit that was never pushed.
`actions/checkout@v4` defaults to `fetch-depth: 1`, so in CI none of the five cited commits were
present and the test failed, while locally it passed against a deeper fetch. All five were verified
present in the object database before anything was changed; **the assertion was not weakened**, the
checkout was too thin to evaluate it. Fixed with `fetch-depth: 0`.

**3. A red run could not say what failed.** Diagnosing #2 required reading the job log, which is
served from a blob store this environment cannot reach. The workflow now re-emits each
`FAILED`/`ERROR` line as a run annotation and writes a short summary to the run page, which is how
#2 was identified at all. That is a durable improvement, not a workaround: a failing job should name
its failures without anyone opening a raw log.

**Observed CI results.** The clean-install smoke job **passed on a real GitHub runner in ~1m07s**,
twice. Ruff lint, ruff format, `compileall` over `src tests migrations`, and the single-alembic-head
gate **passed on both CPython 3.11 and 3.14**. The test job failed at commit `7a3041a` in 20 s
(gap #1), then at `f6d3da2` after ~18 min with exactly one failing test (gap #2, named by the new
annotations).

### 41F. Unverified at the time of writing — stated plainly

The `fetch-depth: 0` fix is committed and was **verified pushed** (`local == ls-remote == 2a5ff5a`,
0 dirty paths). Its CI outcome is **not** known: `GH_TOKEN` expired during the wait
(`gh auth status` → "authentication failed", and the git HTTPS transport then failed with "could not
read Username"), so neither the run status nor its annotations could be read. The claim is therefore
*the fix is published and locally verified*, **not** *CI is green*. Nothing was reset, and no work
was lost. Re-checking the run for `2a5ff5a` is the first thing to do once GitHub authentication is
restored.

### 41G. Final gates, executed after the last change

| Gate | Command | Result |
| --- | --- | --- |
| Full suite | `pytest -q` (bare console script — the form CI uses) | **1658 passed / 3 skipped / 1661, 0 failed, 0 errors, exit 0** |
| Lint | `ruff check .` | All checks passed |
| Format | `ruff format --check .` | 237 files already formatted |
| Byte-compile | `python -m compileall -q src tests migrations` | exit 0 |
| Whitespace | `git diff --check` | exit 0 |
| Migrations | `alembic heads` | `0012 (head)` — no migration added |
| Report integrity | `pytest tests/unit/test_report_integrity.py` | 5 passed |
| Clean-install smoke | `python tools/release_smoke.py` | **PASSED** — wheel and sdist, every check |
| CI, clean-install job | GitHub runner | **PASSED**, ~1m07s, twice |
| CI, lint/format/compile/head gate | GitHub runners, cp3.11 + cp3.14 | **PASSED** on both |
| CI, test job | GitHub runners | **failing at `f6d3da2`** for the depth-1 clone reason; fix pushed at `2a5ff5a`, outcome unverified (§41F) |

Running the suite through the bare `pytest` console script rather than `python -m pytest` is
deliberate: that is the invocation CI uses, it is the one that was broken, and a green run through it
is the only evidence that the `pythonpath` fix actually holds.

### 41H. Release-readiness verdict

The repository is **not** being declared production-ready, and the reason is specific rather than
cautious.

What is now true and was verified by execution: a clean wheel and a clean sdist both install outside
the source tree, import without leakage, bootstrap their schema with no `migrations/` directory,
create and read back a well, run every read surface, and pass `doctor` with exit 0; the documented
CLI flags reach the commands in both placements; the path and symlink boundary holds; the optional
Qt and vector extras are genuinely optional; lint, format, byte-compile and the single-head
migration gate pass on both supported runtimes.

What is **not** yet established: the CI test job has not been observed green. It failed for one
identified, understood and fixed reason — a depth-1 checkout cannot evaluate an assertion about
commit existence — and the fix is published, but GitHub authentication expired before the run could
be read. Until that run is seen passing, "CI is real" is true for the smoke, lint, format,
compile and migration gates and **unproven for the test job**. That is the single item standing
between this repository and a defensible production-ready claim.

### 41I. Correction — the CI test job is green, and ADR-0002 is now demonstrated

§41F and §41H were written while `GH_TOKEN` was expired, and both said the CI test job's outcome was
unverified. **That is now resolved, and the earlier hedging is superseded by measurement.**

Authentication returned, the two pending documentation commits were pushed
(`2a5ff5a..baa6397`, verified `local == ls-remote`, 0 dirty), and run `36559965850` — the run for
the `fetch-depth: 0` fix — completed **success**:

| Job | Result | Duration |
| --- | --- | --- |
| `clean install smoke` | **PASSED** | 1m06s |
| `test (cp3.11)` — the ADR-0002 floor | **PASSED** | 19m23s |
| `test (cp3.14)` — the ADR-0002 product target | **PASSED** | 16m29s |

So the `fetch-depth: 0` diagnosis was correct and complete: the depth-1 checkout was the entire
cause, and no test was weakened to get there. **ADR-0002's claim that "two runtimes are exercised in
CI as the floor moves" is now true and demonstrated on GitHub runners**, where before this mission
it had nothing behind it at all.

Two runner advisories are recorded but are not failures and were not acted on: `actions/checkout@v4`
and `actions/setup-python@v5` are flagged for Node.js 20 deprecation (they are being force-run on
Node 24 and work), and `ubuntu-latest` migrates to Ubuntu 26 on 2026-10-19. Both are worth a
deliberate version bump in a later change rather than an opportunistic one now.

**The release-readiness verdict in §41H is therefore upgraded on its one open item:** clean install,
schema bootstrap, supported-runtime path, CI/reproducibility, public CLI contracts, optional
integration failure and the filesystem boundary are all verified by execution, and the test suite is
green in CI on both supported runtimes.

## 42. V6.1 — closing the remaining production-readiness phases

§41H left one item open and §41 left several phases unexamined. This section records what those
phases produced. Everything below is from execution in this checkout, not from a prior report.

### 42A — a stale extraction was returned after a forced re-extraction

**PRODUCTION BUG FOUND AND FIXED.** `DocumentsRepository.extraction_for_version` read

```
select(Extraction).where(Extraction.document_version_id == version_id).limit(1)
```

with **no `ORDER BY`**. A version can legitimately carry more than one extraction, because
`ingest --force` re-extracts a file whose content has not changed — a documented flag — leaving
several rows for the same `document_version_id`. With no ordering, SQLite scanned in insertion
order and returned the **oldest**: precisely the row the forced re-extraction existed to replace,
with no diagnostic that a choice had been made at all.

Proven against the real pipeline and a real database: after one ingest and one forced re-extract
carrying different content, the method returned the original extraction. Its sibling
`latest_extraction` already ordered by `created_at DESC`, so the two readers disagreed about
"newest" for the same data — the asymmetry is what marks this an oversight rather than a design
choice.

**Why it is not cosmetic.** This reader sits behind knowledge derivation
(`knowledge/service.py:407` and `:723`) and the search index (`search/index.py:545`). A stale
extraction therefore propagates stale text into derived facts and into what search can find: an
operator forces a re-extract to pick up an extractor fix and silently keeps reading the pre-fix
content.

**Fixed at the authoritative layer** — newest-first ordering with `id` as a deterministic
tiebreaker, matching `latest_extraction`. The tiebreaker is load-bearing: rows written inside one
transaction can share a `created_at` resolution, and an unordered query over equal timestamps
drifts between runs.

### 42B — the platform advertised a search engine it does not have

**RELEASE GAP FIXED.** The entire `[search]` settings section — `vector_store`,
`keyword_results`, `semantic_results`, `hybrid_results`, `embedding_cache` — was parsed, validated
against an allow-list, shipped in `configs/development.toml`, and then **read by nothing**: no code
outside `settings.py` touches `settings.search`. Meanwhile `docs/DECISIONS.md:577` states there is
"no second search engine, no second ranking, and no embedding/vector path", and the bounds that
actually govern search are `MAX_CANDIDATES` and `RETRIEVAL_CAP`.

So an operator could set `search.semantic_results`, install the `vec` extra and believe they had
hybrid recall. **The absence of vector search is an intentional product boundary and is not called
a defect**; advertising it through an installable extra and validated knobs is the problem, and is
classified `UNSAFE_TO_CLAIM`.

`pyproject.toml`'s own dependency rule already forbids this — "a library listed here and not used is
not free: it is a supply-chain surface … and a promise about a capability the platform does not
have" — so the never-imported `vec` extra was removed **on the project's own stated grounds**.
`sqlite-vec` was verified imported nowhere in `src/` or `tests/`. The config keys were kept rather
than deleted, because `Settings` tolerates unknown keys and an operator TOML must not break; they
are now labelled in the shipped config as accepted-but-unused, pointing at the bounds that do
govern search. The shipped config was re-verified to load with no unknown keys reported.

### 42C — the optional-integration boundary had no test at all

**COVERAGE GAP FIXED.** Nothing imported `integrations.mineru` or `integrations.base`;
`pytest.mark.mineru` and `pytest.mark.network` were declared in `pyproject.toml` and used zero
times. A regression could have made the prober lie and nothing would have noticed.

Exercised directly, the boundary is sound: with no runtime present, `MinerUProber.available()`
returns `False` **with a reason** naming both transports it tried
(`executable 'mineru' not found on PATH`; `no HTTP response from http://127.0.0.1:8000`), each
carrying its own explanation, plus honest limitations — including that MinerU's output is
layout-based so cell-level XLSX provenance is unavailable and XLSX stays on the native openpyxl
extractor. That contract is now pinned. **Absent means named-absent, never silently usable.**

### 42D — ingestion recovery under interruption

**COVERAGE GAP FIXED.** The module already covered first run, second-run no-op, changed file
becoming a new version, duplicate detection, limited runs and removed files. It did not cover
stopping part-way. Two contracts are now pinned, both asserting the end state equals the state an
uninterrupted run produces, compared as exact identity tuples rather than counts — counts would
hide a churned extraction id behind an unchanged total.

- A run stopped through the **supported `cancel` callback** converges on the next run.
- A run in which **one file fails** converges too, and the failure is *reported*, not raised: the
  pipeline converts a per-file failure into a reported failure rather than aborting the scan, so
  the contract worth pinning is visibility plus convergence, not exception propagation.

Two premises were disproved while writing this, and are recorded so they are not retried: patching
`workspace_identity` never fires, because `run` passes an explicit `workspace_id` and the pipeline
only resolves one when the caller omits it; and a re-run over an unchanged corpus never reaches
`DocumentRegistry.register` at all, which is the documented second-run no-op, so `--force` is
required to exercise a mid-run fault.

### 42E — domain-expansion readiness (audit only)

**AUDIT — no defect, no feature added.** Domain expansion is governed by
`operations/contracts.py`, which states its own rule: the registry has no callable plugin
mechanism, and "adding a new domain writer therefore requires a visible registry entry, a writer,
and a test/certification update". `CONTRACTS` maps **all 26** `DocumentClassification` members, and
an import-time guard raises `RuntimeError` naming `missing=` and `extra=` if the registry and the
enum ever diverge — exercised by `tests/unit/test_promotion_contracts.py` importing it.

Read off the real registry:

| coverage level | count | classifications |
|---|---|---|
| `END_TO_END_CERTIFIED` | 8 | DDR, NPT, TIME_BREAKDOWN, DRILLING_PROGRAM, MUD_REPORT, BHA_REPORT, BIT_RECORD, DIRECTIONAL_SURVEY — each with a named handler and real target models |
| `KNOWLEDGE_SUPPORTED` | 13 | CASING_REPORT, CEMENT_REPORT, COST, WELL_CONTROL, LOGGING, HSE, EOWR, SERVICE_REPORT, PROCEDURE, STANDARD, CONTRACT, TECHNICAL_REFERENCE, LESSON_LEARNED — all `handler=-` |
| `EXTRACT_ONLY` | 5 | BOOK, INVOICE, LWD_MWD, WIRELINE, OTHER |
| `CLASSIFY_ONLY` / `DOMAIN_PROMOTABLE` / `REVIEWABLE` / `UNSUPPORTED` | 0 | — |

Every domain §34 named — **COST, CASING, CEMENT, WELL CONTROL, LOGGING** — is already a first-class
classification that classifies, extracts and feeds knowledge and evidence, but has **no domain
writer**. That is an explicit boundary, not a silent gap: the module states that classifications
without a contract "are not guessed into the nearest operational table". Expanding one means a
registry entry, a `VersionPromoter` writer method, a migration for its target tables, and a
certification update. `PLANNED FEATURE`, and the framework is ready for it.

### 42F — what remains

No phase of the V6.0 mission is left unexamined. The items deliberately **not** done, and why:
the dead `SearchSettings` fields were annotated rather than deleted, because `Settings` tolerates
unknown keys, so removal would be graceful but is a product decision and not a defect fix; the
`ui`/`network`/`mineru` markers remain declared with almost no users, which is harmless and cheap
to keep. No test was weakened anywhere in this section: the extraction bug was found by a test
that first failed for the right reason.

## 43. V6.2 — adversarial runtime-boundary certification

Every statement here comes from execution in this checkout.

### 43A — defects found and fixed

| # | defect | root cause | fix | mutation |
|---|---|---|---|---|
| 1 | `doctor` printed `ai  ollama / model qwen3:8b, required=False` with `findings none`, exit 0, on a build that cannot talk to Ollama | `Settings.summary()['ai']` reported the config as though it were wired; nothing in `src/` reads `settings.ai` and no provider class exists | `AI_PROVIDER_IMPLEMENTED` constant drives `implemented`/`note` on both the JSON and text surfaces | killed (flag flipped; CLI suffix dropped) |
| 2 | A MinerU exit code of 0 was recorded as -1 | `int(run.get("returncode", -1) or -1)` — 0 is falsy | explicit `None` check | M62-1 killed |
| 3 | Output from a crashed MinerU run was indistinguishable from a clean parse | acceptance on non-zero exit was deliberate but silent; `MinerURun` carried no exit status | `returncode` on the record and in `to_dict()`, an explicit `error` note, an adapter diagnostic | M62-3, M62-4 killed |
| 4 | A well-formed HTTP response with an empty result became a document with no text, no sections, and a diagnostic claiming "MinerU markdown used" | any dict `results` entry fell through to `normalize_markdown("")` | refused at the external boundary | M62-2 killed |
| 5 | `latest_extraction` disagreed with `extraction_for_version` about which row was newest | ordered by `created_at DESC` with no tiebreaker; equal timestamps resolve by scan order | `created_at DESC, id DESC`, matching the convention already used in promote/service/review/engineering/lessons | M62-5 killed |

Two documentation claims contradicted the code and were corrected rather than deleted: the README
grouped Ollama with `mineru` as if both were optional integrations you could switch on, and ADR-0005
stated that `[ai] provider = "none"` is the development default when the shipped config, the
dataclass and bare defaults all say `ollama`.

### 43B — a suspected defect that was investigated and rejected

Stamping `0011` onto a database that already carries the `0012` schema makes the upgrade fail on
`index uq_calculation_one_superseding_revision already exists`. That state is reachable only by
hand-editing `alembic_version`, so it is a database lying about itself, not a product defect. The
genuine one-revision-behind case — built by running the real migrations to 0011, verified to have 45
tables and no 0012 index — upgrades cleanly.

### 43C — boundaries exercised

Ingestion recovery (cancel, per-file failure, force re-extraction, equal-timestamp ordering); MinerU
discovery in all four modes plus CLI execution across a **real subprocess** and HTTP across a
transport double; the review workbench's Qt-free controller layer; filesystem and process boundaries
(symlink escape, hostile filenames, argv construction, environment propagation, malformed
endpoints); SQLite concurrency under real threads; `doctor` across eight database states; wheel and
sdist release smoke.

### 43D — explicitly not exercised, and why

The **Qt-present** UI contract could not be exercised: `PySide6-Essentials` installs, but
`QtWidgets` needs the native `libGL.so.1`, and the sandbox has no route to it (the apt mirrors are
unreachable). The skip reports the dynamic-linker reason, so it is distinguishable from "Qt not
installed" and there is no UI-success state. `ReviewController` imports no Qt at all, so the logic
that decides what the workbench shows and may do is certified in the default headless suite instead.

One real race is recorded rather than fixed: two processes opening a *fresh* workspace
simultaneously race the one-time schema bootstrap, producing `table alembic_version already exists`
and a bare `KeyError: 'config'`. It is outside the documented single-operator scope, and the
outcome is a loud failure rather than corruption or a misleading success. Fixing it would mean
touching schema bootstrap, the riskiest area in the repository, for a case the architecture does not
claim to support.

## 44. V7.0 — cost admitted as a certified domain writer

Every statement here comes from execution in this checkout.

### 44A — recovery, and the closure of the V6.2 publication gap

The workspace came back grafted at `e862113` with the accumulated V4-V6.2 work sitting uncommitted
on top of it. Staging it showed the tree differed from the remote tip in **exactly one file**: the
51-line section 43 record. That was the content of the commit that never published, still alive in
the worktree. The branch was fast-forwarded to `f415991` with the section set aside, then restored
and committed as `ccdffa3` - no reset, no rebase, no history rewrite, and no attempt to reproduce
the old SHA.

`f415991` is now confirmed **success** in CI, which closes the one V6.2 item that had only ever been
observed as `in_progress`.

### 44B — cost: KNOWLEDGE_SUPPORTED to END_TO_END_CERTIFIED

`CostItem` is reused, not duplicated - it already carries a code, a description, planned and actual
amounts each with their own unit, a scope, provenance and a content-derived identity. No migration.

The contract is narrow: a cost code column, a description column, and a money column whose header
states planned-side or actual-side. Prose quoting an amount is refused; so is a table headed merely
`Amount`.

### 44C — defects found and fixed

| # | defect | root cause | fix |
|---|---|---|---|
| 1 | The cost writer could never report `PROMOTED`, and `total()` would have raised on it | `result.counts` was assigned a bare int, but it maps a kind to a created/unchanged/conflict bucket and `finalize()` derives `PROMOTED` from `wrote_anything`, which reads those buckets | `result.bump("cost_item", ...)` |
| 2 | No cost table was ever recognised, so every ledger was `UNSUPPORTED` | The header was assumed to be `rows[0]`. An extractor stores the whole sheet region, including the title and currency note above the column headings, so the title was read as a header and the real table as data | `locate_cost_header` scans the first eight rows, matching the survey and BHA convention |

Defect 2 is worth noting as a class: a parser written against a hand-built payload passes every unit
test and reads nothing from a real extraction. It was only visible on the real ingest path.

### 44D — explicitly not done

`CASING_REPORT` and `CEMENT_REPORT` remain `KNOWLEDGE_SUPPORTED` with no writer. Neither has a
model: `WellSection.casing_program` is a 120-character label on a hole-section record, not a casing
run, and "cement" appears in the codebase only as vocabulary. Both need a new table, a migration
and a writer. The registry was not touched for them, and no placeholder rows were added.
