# MISSION_RESULT — V5.1: NPT attribution and plan-vs-actual match provenance

> Commits are cited in short form; `tests/unit/test_report_integrity.py` enforces that a report never
> names a 40-hex hash the local object database cannot produce.

## 1. Repository identity

| fact | value |
|---|---|
| repo | `asgareyvazi/Prog-Proc` |
| branch | `arena/01a0c936-prog-proc` |
| local HEAD | `cd04c2d` |
| remote HEAD | `cd04c2d` — **verified by `git ls-remote`** |
| baseline | `5fc4c85` (present locally, `git cat-file -e` confirms) |
| ahead/behind baseline | 4 ahead / 0 behind (2 doc + 2 source commits) |
| worktree | clean, `git status --short` returns 0 lines |

No environment reset this session — unlike the previous six, HEAD and the worktree were exactly as
left. The two V5.0 report commits that were blocked by an expired token at the end of the last
session were published at the start of this one (`5fc4c85..24118cb`, exit 0).

## 2. Merge verification (§0.2, §1)

No new merge this session; the branch tip is linear from the baseline. The previously certified
boundary was smoke-tested rather than re-audited, as §1 directs:

* **V5.0 invariants re-proved green** — `test_intelligence_forensics.py` + `test_field_intelligence.py`,
  84 tests, exit 0. All six V5.0 regression tests are present in source (snapshot freeze, candidate
  refresh, staleness widening, exact grouping lookup, event fan-out, unknown duration).
* **Workspace/promotion boundary** — identity_v45, boundary_v46, binding_v45c, relocation_safety_v46,
  structured_index_boundary, document_invariants: **92 tests, exit 0**. Frozen, untouched.
* `alembic heads` → **`0011`**, unchanged. No migration added (§62).

## 3. Defects found

### 3.1 §50 — an incident's NPT hours were multiplied across the problems on it

**Failure.** Two problems sharing one event each received that event's full NPT. Measured:

```
PROBE §50 rows=2 per_problem=[('pr-p1', 6.0), ('pr-p2', 6.0)]
PROBE §50 SUM over problem_hours = 12.0   (the event only carries 6.0h of NPT)
PROBLEMS -> npt_hours=12.0   find_recurring -> total_npt_hours=12.0
```

**Reproduction.** One `WellEvent`, two `ProblemOccurrence` rows pointing at it with `npt_id=NULL`,
two `NptRecord` rows (2 h + 4 h) on the same event.

**Root cause.** V5.0 collapsed the event path to one row per problem, which fixed *one problem
coming back several times*. It did not address the other direction. Both consumers
(`FieldIntelligence.problems`, `find_recurring`) `SUM` this subquery, so an incident-level quantity
was spread across problem-level rows and then added up.

**Semantics — decided from the model, not convenience.** `ProblemOccurrence.npt_id` is a direct FK
to one NPT record: the source *stating* "this problem caused that record". `event_id` only says the
problem and the NPT belong to the same **incident** — it never says which problem on that incident
caused which record. With one problem on the event the attribution is unambiguous and is kept (V5.0's
behaviour, still tested). With several it is genuinely ambiguous: attributing the hours to each
asserts more than the source states, and splitting them would invent a proportion nobody wrote down.

**Fix.** The event path attributes only when the problem is the sole problem on its event. The
withheld set is queryable via a new `ambiguous_event_attribution()` and counted in the field
aggregation as `unattributed_npt_problems` (per type and in total). After the fix:

```
rows=0   total_npt_hours=None   occurrence_count=2
ambiguous set = [('pr-p1','evt-multi'), ('pr-p2','evt-multi')]
unattributed total=2  for shared_event=2
```

The hours are **unknown, not zero**, the problems still count, and the hours remain visible in the
event- and NPT-scoped aggregations where they are not ambiguous. The omission is named, not silent.

**Regression tests.** `test_two_problems_on_one_incident_do_not_each_receive_its_npt_hours`,
`test_a_pattern_is_not_inflated_by_several_problems_sharing_one_incident` (§51 — same join from the
pattern side: `occurrence_count=2`, `event_count=1`, `total_npt_hours=None`).

### 3.2 §14/§15 — a plan matched by name was indistinguishable from an exact join

**Failure.** `_match_target` matched on section id first and fell back to the section **name**. Its
docstring claimed a name match "is still reported without its id" so the two could be told apart.
That was **false** — the row carried `target_id` either way. And when two governing programmes both
carried a target of the same name, the precedence order silently picked one, so two plans
disagreeing about the same section read as a single plan.

**Measured.**

```
field template            -> planned=[5000.0] matched_by absent
two equally-ranked plans  -> planned=[1111.0] (picked silently, no signal)
```

**Fix.** `_match_target` now returns the match *and* how it was found; every row carries
`matched_by`: `SECTION_ID` (exact join), `NAME` (pre-spud template fallback), `NAME_AMBIGUOUS`
(more than one governing programme claims the section name), `""` (nothing matched).

```
field template           -> matched_by=['NAME']
two equally-ranked plans -> matched_by=['NAME_AMBIGUOUS']
```

**Regression tests.** Four, in a new `TestMatchProvenance` class.

## 4. Suspected defects disproved

Mandatory section — three hypotheses were tested and did **not** reproduce:

1. **§15 cross-well plan leakage — DISPROVED.** A programme owned by well B with an unbound target
   named `12 1/4 in` cannot supply the plan for well A's identically-named section, even with no
   `program_id`. Measured `planned=[None]`, `target_id=[None]`, `matched_by=''`, `status=NO_TARGET`.
   The target side is confined to programmes governing the subject wells
   (well-owned → field-owned → project-owned). Now pinned by
   `test_a_foreign_wells_program_still_cannot_supply_the_plan`.
2. **§26 review writes — DISPROVED, and already proven properly.** `review/service.py` contains no
   `flush`/`commit`/`delete`; its single `.add(` is `seen.add(key)` on a Python set. More
   importantly `test_review_is_authoritative_repeatable_and_does_not_mutate` already takes a
   full-table snapshot (`SELECT * FROM <every table> ORDER BY rowid`) before, between two reviews
   and after, and asserts all three are equal — a real fingerprint proof, not a comment.
3. **§6 unknown duration — correct as implemented** (carried from V5.0, re-verified green):
   `records` minus rows carrying a duration, so `2 h / 4 h / NULL` reports `records=3, hours=6.0,
   unknown_duration=1`.

## 5. Problem-hours attribution — the four cases (§76.10)

| case | rows | hours | semantics |
|---|---|---|---|
| one problem / one event / one NPT | 1 | that NPT's hours | direct `npt_id`, source-stated |
| one problem / one event / many NPT | 1 | sum of the event's NPT | unambiguous — one candidate problem (V5.0) |
| many problems / one event | **0** | **not attributed** | ambiguous — named via `ambiguous_event_attribution()` |
| mixed direct + event paths | direct rows only | direct hours | the stated link wins; the ambiguous one is withheld |

## 6. Mutation results

| mutation | result |
|---|---|
| M1 — never report `NAME_AMBIGUOUS` | **caught** (1 failure) |
| M2 — drop the governing-programme scope from the target side | **caught** (1 failure) |

Attempted **2**, caught **2**, survived **0**. Both reverted; `git diff --stat` confirms the tree
returned to the intended change.

## 7. Full test result

```
collected  1538
passed     1535
skipped    3
failed     0
errors     0
exit code  0
```

1538 = V5.0's 1532 + the 6 tests added this session. Counts taken from a regex over the progress
lines, because `pytest -q` under a redirect emits no summary line here.

## 8. Quality gates

```
ruff check src tests migrations          exit 0   All checks passed!
ruff format --check src tests migrations exit 0   209 files already formatted
python -m compileall -q src tests migrations      exit 0
git diff --check                                  exit 0
alembic heads                                     0011 (head)
```

No unrelated formatting churn: `ruff format` was run only on the four files this mission touched.

## 9. Git

```
cd04c2d  V5.1 §14/§15: match provenance          <- HEAD, remote-verified
362a557  V5.1 §50/§51: NPT attribution
24118cb  docs: V5.0 report push status
632ccea  docs: V5.0 forensic report
5fc4c85  <- baseline
```

Changed files: `intelligence/field.py` (+70), `engineering/repository.py` (+19/−11),
`test_field_intelligence.py` (+150), `test_plan_actual_forensics.py` (+104), plus the V5.0 report
document. Every change claimed here exists in Git and is on the remote.

## 10. Final verdict

**RELEASE-CERTIFIABLE** — for the scope actually exercised this session.

The tree is green on every gate that was run, the worktree is clean, and remote publication is
directly verified by `git ls-remote`. No known defect is outstanding in the areas audited.

**Explicitly not exercised this session**, and therefore not certified by this report: calculation
scope integrity (§3–§13), timeline (§21–§25), review current/history and truncation matrices
(§27–§31), retrieval authority (§32–§34), evidence packages and citation verification (§35–§40),
promotion contracts and idempotence (§41–§49), child-row identity (§45), concurrency (§59),
performance (§60) and the query-limit audit (§61). These are open work, not passing gates — a future
report should not read this one as covering them.
