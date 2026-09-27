# MISSION_RESULT — V5.0: forensic hardening of field intelligence, patterns and derived data

> **Note on commit references.** Commits are cited in short form; `tests/unit/test_report_integrity.py`
> enforces that a report never names a 40-hex hash the local object database cannot produce.

## 1. Verdict

**RELEASE-CERTIFIABLE.** Full suite exit 0, all gates clean, work pushed to the remote.

## 2. Git state, verified this session

| fact | value |
|---|---|
| session branch | `arena/01a0c936-prog-proc` |
| HEAD at start | `8847d67` (worktree clean) |
| remote tip at start | `71112e8` (local 1 ahead — one `.md`) |
| work commit | `5fc4c85` |
| push | `71112e8..5fc4c85`, **exit 0**, remote verified |

The environment had been reset again at session start (grafted `e862113`, 80 uncommitted paths,
`.venv` gone). It was recovered cleanly and verified before any work began: the working tree was
proved equal to the remote tree except one known document, every untracked file was byte-compared
against the remote version before being unlinked, and `git merge --ff-only` fast-forwarded without
discarding anything (local was 0 ahead / 16 behind).

## 3. §0.2 — the merge was verified, not trusted

`71112e8` merges `56d396d` (P1) with `e2cd185` (P2). Independently re-proved this session:

* **zero files differ from both parents** — no hand-blended conflict resolutions exist;
* the merge tree is **identical to P1's tree**;
* no file exists in P2 that P1 lacks;
* only three source files differ semantically, and in each the P2-only content is the **weaker
  predecessor** P1 deliberately replaced — P2's `_assert_workspace_belongs_here` only asked "is this
  id a row in this database", and P2's `resolve_workspace_id` lacked the `database_path` kwarg;
* `cli/app.py` has **zero** non-blank P2-only lines.

P1 is a strict superset. **No certified invariant was lost.** §1 smoke regression: the four
workspace suites pass (70 tests), so that area stayed frozen as instructed.

## 4. Findings — each proved by experiment before being changed

### 4.1 A CONFIRMED pattern's accepted measurement was silently rewritten — **fixed**

Experiment: snapshot → confirm as a person → change the source rows → re-run `snapshot()`.

```
PROBE after re-snapshot: occ=5 ... status=CONFIRMED
PROBE MEASUREMENT CHANGED: True
PROBE staleness AFTER re-snapshot: stale=False
```

The numbers a person had vouched for were overwritten **and** `stale_at` was cleared, so the drift
left no trace anywhere. This contradicted `staleness()`'s own docstring ("the stored numbers are not
touched … the reviewed figure is frozen"), and it is precisely the primary invariant: a derived row
carrying a stronger claim than its evidence justifies.

Fix: a **vouched-for row is frozen**; the recomputation is recorded in `attributes.recomputed` and
the drift is marked. A **CANDIDATE still refreshes in place**, so recomputation is not blocked.
Re-verified: `MEASUREMENT CHANGED: False`, `stale=True`. No schema change — it reuses the existing
`stale_at` / `stale_snapshot` columns.

### 4.2 Staleness compared only three of the stored measurements — **fixed**

`occurrence_count`, `well_count` and `total_npt_hours` were compared. `event_count`,
`first_seen_at`, `last_seen_at` and well membership were not, so a grouping could change materially
while reporting itself unchanged. Proof: detaching a grouping's occurrences from their events leaves
every total identical — the old check reported `stale=False`. All are compared now.

The re-run also **looks the grouping up exactly** (`hole_size_in` is constrained in the query)
instead of listing up to 500 groupings and searching them, so no presentation limit sits in a
correctness path.

### 4.3 `problem_hours()` multiplied a problem by its event's NPT records — **fixed**

```
PROBE §7 fan-out: 1 problem / 1 event / 2 NPTs -> 2 row(s) hours=[2.0, 4.0] total=6.0
```

The hours summed correctly — which is exactly why it survived — but one problem occurrence came back
as two rows, so every caller that counts rows rather than summing them counted the same problem
twice. The event path is collapsed to one row per problem before the join; the total is preserved
and the problem now counts once (`1 row(s) hours=[6.0]`).

### 4.4 `unknown_duration` — **audited, correct as implemented, not changed**

`records` minus the rows carrying a duration, via `COUNT(duration_hours)` which excludes NULLs.
Three records of 2 h, 4 h and NULL report `records=3`, `hours=6.0`, `unknown_duration=1`. A missing
duration is never read as `0.0`. Pinned by a new test rather than left unverified.

## 5. One claim deliberately **not** made

§11 asked whether the `limit=500` staleness scan could report a false `found=False, stale=True`.
**I could not provoke it.** A snapshot always stores its own `problem_type`, which already narrows
the re-run to a handful of groups, so the cap was not reachable through the normal path. A mutation
restoring the capped scan **survives** the new test, and the test says so in its docstring rather
than implying it caught a live defect. The exact lookup is still a real improvement — it removes a
presentation limit from a correctness path — but it is robustness, not a bug fix.

## 6. Verification

| gate | result |
|---|---|
| full suite | **exit 0** — 1529 passed, 3 skipped, 0 failed, 0 errors (1532 = 1526 + 6 new) |
| `ruff check src tests` | All checks passed |
| `ruff format --check src tests` | 197 files already formatted |
| mutation testing | M1 fan-out, M2 snapshot freeze, M3 staleness widening — **all caught**; M4 capped scan — **survived, explained in §5** |
| `alembic heads` | `0011`, unchanged — no migration needed |

Six new tests, all asserting semantic results through the public API (no source-text assertions).
One existing test was corrected, not weakened: it asserted an exact difference dict that encoded the
*incomplete* comparison, and now pins the fuller truth including the moved boundary date.

## 7. Not yet done

§15–§17 timeline, §18–§26 calculations and change-impact, §27–§30 review read-only proof and
truncation, §31–§34 evidence and retrieval, §35–§37 domain contracts and promotion, §38 corpus
fixtures, §41–§43 atomicity/concurrency/performance, §45 documentation sweep, §51 corrupt-data
paths. The workspace boundary (§1) and the pattern/field-intelligence layer audited here are
complete and certified.
