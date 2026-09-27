# Arena V4.3 retroactive semantic repair — last result

**Date:** 2026-09-23 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0c936-prog-proc`
**HEAD before this mission (remote):** `c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9`
**V4.3 work commit:** the commit carrying this file —
`git log -1 -- docs/ARENA_V4_3_RETROACTIVE_SEMANTIC_REPAIR_LAST_RESULT.md`
**V3 baseline:** `b7703baf35abd84881432a93264a979c33751c6c` — **not** an ancestor of HEAD, and not
present in this shallow clone's object database (see §9).
**Authority:** the checked-out repository. Every figure below comes from a command run during this
mission.
**Design document:** `docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md` (§13–§18 are new or rewritten)

---

## 0. The workspace was not where the mission assumed

The mission states the previous mission ended at `c279d1c`. It was told to verify that, and verifying
it found something else:

```
git rev-parse HEAD                       -> e8621136ca73108ae7b590e6baa72fedc1f00835   (grafted)
git ls-remote origin refs/heads/...      -> c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9
git status --short                       -> 28 modified/deleted + 23 untracked
```

The local branch had been re-created from the grafted baseline `e862113` with all V4.x work present
only as **uncommitted** working-tree changes, while the remote still held the pushed V4.2 commit.

Established before touching anything:

* `e862113` **is** an ancestor of `c279d1c` (linear, no divergence);
* `git diff c279d1c` showed **only** the 23 untracked files — no tracked file differed;
* all 23 untracked files were hashed against their `c279d1c` blobs: **23/23 identical**.

So the working tree was already exactly `c279d1c`'s tree and the branch was merely behind. Recovery
was a fast-forward, not a rewrite: the files were backed up to `/tmp/v43_backup`, `git stash -u`
(recoverable, never `reset --hard`), `git merge --ff-only c279d1c`, then every backup re-hashed
against the restored tree — **23 compared, 0 problems**. A safety tag
`v43-safety-local-head -> e862113` and the stash both remain. No history was rewritten and nothing
was force-pushed.

---

## 1. Verdict

**CERTIFIABLE_WITH_DOCUMENTED_LIMITATIONS.**

The central claim of V4.2 — that predicate repair is *not* retroactive — was **wrong**, and V4.3
disproves it. The semantic context was recorded all along in `provenance.excerpt`; it was simply
never consulted. `knowledge rebuild` is now genuinely retroactive, deterministic, idempotent by
construction, and it never rewrites the artefact it reads.

Two further defects were found and fixed at their boundaries: table headers discarded semantic
qualifiers, and one engineering quantity ("total mud volume") had two identities depending on which
extraction path it arrived by.

Limitations are in §8; they are real and none is hidden by a passing test.

---

## 2. Reproducing the V4.2 state (before changing anything)

| Check | Observed |
|---|---|
| `len(PREDICATES)` | **34** |
| `len(PREDICATE_BY_FIELD)` | **89** |
| `len(UNIT_SUFFIX_TOKENS)` | **19**, with `md`/`tvd` absent |
| V4.2 suites | `test_knowledge_semantic_vocabulary_v42.py` **24 passed**; `test_knowledge_conflict_semantics_v4.py` **6 passed**; `test_knowledge_facts.py` **23 passed** |
| Schema changes in V4.2 | none — `git diff --name-only` touches no `models.py` or `alembic/` file |
| Report integrity | **2 failed** — see §9; a shallow-clone fragility in the test, not a documentation defect |

The two report-integrity failures were traced before being treated as anything: the clone is shallow
(`git rev-parse --is-shallow-repository` → `true`, 7 reachable objects), so pre-boundary SHAs such as
the V3 baseline `b7703baf` **cannot** exist locally. The test could not distinguish "the report named
a bogus SHA" from "this clone never fetched it". Fixed by skipping with an explicit reason under
shallowness while keeping the strict form for a complete checkout; it now reports
**2 passed, 2 skipped** with the missing SHAs named in the skip message.

---

## 3. The retroactive repair architecture

### What the audit found

There is exactly **one** `from_field` call site — `knowledge/service.py`, inside
`facts_for_version`, reached by both ingest and `rebuild`. Its input is
`extraction.document_json["extracted_fields"]`, so the rebuild's semantic input is the **stored field
name**. That part of V4.2's reasoning was correct.

What it missed is what else is stored. Every entry carries:

```
name, value, unit, dimension, confidence, method, quality, note
provenance: {document_id, document_version_id, filename, parser, excerpt, locator, source_sha256}
```

`excerpt` is the span of source text the value was read from. From the live V4 corpus:

```
depth_md           10125  ft    "MD 10125 ft"
depth_tvd           9850  ft    "TVD (ft) 9850 ft"
sidpp                420  psi   "SIDPP 420 psi"
maasp               1850  psi   "Maximum allowable annular surface pressure was limited to 1,850…"
rheometer_speed      300  rpm   "Rheometer reading at 500/300 rpm"
mud_volume_bbl      1450  bbl   "1,450 bbl total system volume"
```

**The context was recorded, not lost.**

### The four options, and why one is right

| Option | Verdict |
|---|---|
| **A. Re-ingest the source documents** | Works, but unnecessary — and the most expensive, since it re-parses every file. Kept as the fallback for the `requires_reextraction` category. |
| **B. Rebuild from stored canonical DataFields** | **Insufficient as stated**: the stored *name* is the collapsed one, so rebuilding from names alone reproduces the old predicate. |
| **C. Explicit backfill mutating existing rows** | Rejected. Mutating `KnowledgeItem.predicate` and `lookup_key` in place risks the identity constraints, needs its own idempotency bookkeeping, and leaves the artefact and the knowledge table disagreeing. |
| **D. Hybrid — stored fields + recorded excerpt + re-derivation** | **Chosen.** Re-run the same deterministic extractor over the recorded `excerpt`, on the way *out* of the artefact, during derivation. |

D wins because it needs no source document, invents nothing (the excerpt is the source's own text and
the extractor is the same pure function that ran at ingest), leaves `document_json` untouched, and is
**idempotent by construction** — recovery is a pure function of stored data, so running it twice
cannot drift.

### What is and is not rewritten

* `Extraction.document_json` — **never**. It remains a record of what the extractor produced at the
  time, so "a rebuild reads what was recorded and gets the same answer years later" still holds, and
  the evidence of what the old extractor actually did survives. Pinned by
  `test_a_rebuild_does_not_rewrite_the_stored_artefact`.
* `KnowledgeItem.predicate` / `lookup_key` — re-derived, as they always were on a rebuild.
* Provenance, `source_sha256`, record state, supersession — untouched. Pinned by
  `test_every_separated_quantity_keeps_its_provenance_through_the_repair` and
  `test_the_repair_does_not_resurrect_or_reorder_history`.

---

## 4. The repair matrix, and the defect it caught

Recovery is keyed to a **closed, auditable table** of the predicates a fix actually split — not a
heuristic over any label found in the excerpt.

```python
SPLIT_PREDICATES = {
    "surface_pressure":  ("sidpp", "sicp", "maasp"),
    "mud_volume":        ("pill_volume", "kick_volume", "trip_tank_volume"),
    "rpm":               ("rheometer_speed",),
    "hole_depth":        ("measured_depth", "true_vertical_depth"),
    "hole_section_size": ("bit_size",),
}
```

| Stored field | Excerpt | Recovers to | Category |
|---|---|---|---|
| `hole_depth` | `MD 10125 ft` | `depth_md` → `measured_depth` | deterministic |
| `hole_depth` | `TVD (ft) 9850 ft` | `depth_tvd` → `true_vertical_depth` | deterministic |
| `hole_depth` | `9,000 ft` | — | **ambiguous** |
| `hole_depth` | *(none recorded)* | — | **requires_reextraction** |
| `surface_pressure` | `SIDPP 420 psi` | `sidpp` | deterministic |
| `surface_pressure` | `SICP 610 psi` | `sicp` | deterministic |
| `surface_pressure` | `MAASP 1850 psi` | `maasp` | deterministic |
| `surface_pressure` | `1850 psi` | — | **ambiguous** |
| `rpm` | `Rheometer reading at 500/300 rpm` | `rheometer_speed` | deterministic |
| `rpm` | `with 120 rpm` | `rpm` (unchanged) | unchanged — stays generic |
| `mud_volume` | `kick volume 12 bbl` | `kick_volume` | deterministic |
| `mud_volume` | `1,450 bbl total system volume` | `mud_volume` (unchanged) | unchanged — confirmed |
| `bit_size` | any | `bit_size` | unchanged — already specific |
| `mud_balance` | `2025-05-30` | `mud_balance` | unchanged — **never demoted** |
| `depth` | any | — | **ambiguous**; a bare depth is not an MD |

### A defect found by building the matrix

The first implementation matched *any* label the extractor found in the excerpt. A dry run over the
real V4 corpus then reported:

```
mud_balance  -> date_iso      deterministic
report_date  -> date_iso      deterministic
```

A mud-balance **calibration date** stored beside `"2025-05-30"` was being "recovered" into the
generic `date_iso`, because a date pattern matched the bare span. That is not a repair; it trades a
correct, specific name for a worse one. No blind global migration would have caught it, and neither
would a test written only against the five collapsed names.

The fix is the closed table above: recovery may **refine** a name the vocabulary collapsed and may
never **demote** one it did not. `test_recovery_refines_and_never_demotes_a_specific_name` pins six
such fields.

Three invariants, each pinned:

1. **Refine, never demote.**
2. **The value locates the reading; the label decides the predicate.** In
   `"SIDPP 420 psi and SICP 610 psi"` the value selects which occurrence the row came from; it never
   chooses the quantity. Two labels with the same number in one span → ambiguous, not a coin flip.
3. **Ambiguity is a result, not a failure.** Reported with its reason; never silently skipped, never
   silently guessed.

---

## 5. Extraction-boundary changes

### `Depth (ft MD)` and `Depth (ft TVD)` — FIXED

`tableshape.without_units` deleted **every** parenthetical unconditionally, before any contract could
read it:

```
"Depth (ft MD)"   -> "depth"        MD destroyed
"Depth (ft TVD)"  -> "depth"        TVD destroyed
"Depth (ft)"      -> "depth"        correct
```

This was an internal inconsistency in one module: `header_unit` already documented that "an
unrecognised parenthetical is a clarification, not a unit", while `without_units` deleted it anyway.

Fixed with a declared qualifier vocabulary:

```python
SEMANTIC_QUALIFIERS = ("md", "tvd")
```

A parenthetical is dropped only when it carries no declared qualifier. Observed after the fix:

| Header | `without_units` | mud contract field | predicate |
|---|---|---|---|
| `Depth (ft MD)` | `depth md` | `depth_md` | `measured_depth` |
| `Depth (MD)` | `depth md` | `depth_md` | `measured_depth` |
| `MD (ft)` | `md` | `depth_md` | `measured_depth` |
| `Measured Depth (ft)` | `measured depth` | `depth_md` | `measured_depth` |
| `Depth MD` | `depth md` | `depth_md` | `measured_depth` |
| `Depth, ft MD` | `depth md` | `depth_md` | `measured_depth` |
| `Depth (ft TVD)` | `depth tvd` | `depth_tvd` | `true_vertical_depth` |
| `TVD (ft)` | `tvd` | `depth_tvd` | `true_vertical_depth` |
| `True Vertical Depth (ft)` | `true vertical depth` | `depth_tvd` | `true_vertical_depth` |
| `Depth (ft)` | `depth` | *unrecognised* | — |
| `Depth` | `depth` | *unrecognised* | — |
| `Depth (m)` | `depth` | *unrecognised* | — |
| `OD (in)` | `od` | — | — |
| `Remarks (optional)` | `remarks` | — | — |
| `Qty (approx)` | `qty` | — | — |
| `Serial No (S/N)` | `serial no` | — | — |

**The negative cases are the important half.** `Depth`, `Depth (ft)` and `Depth (m)` stay `depth` and
are *not* promoted to `depth_md`: a bare depth is ambiguous and manufacturing `md` would be inventing
a measurement. `canonical_summary_label` returns `""` for all three.

The contracts already accepted `"depth md"` (`survey.py`, `mud.py`); only `without_units` destroyed it
first. `"depth tvd"` was added to the mud and survey alias tables for symmetry — its absence was a
latent MD/TVD merge on the TVD side only.

The qualifier list is deliberately two entries wide. `"Pressure (psi SIDPP)"` still normalises to
`pressure`: an undeclared qualifier does not survive, so the vocabulary cannot grow by accident.

### The class of defect, not just the instance

`header_unit` reads a parenthesised unit only when the parenthetical *is* a declared unit, so
`Remarks (optional)` never becomes a unit. `without_units` was the only place that deleted
parentheticals indiscriminately, and it is now consistent with `header_unit`. `DEFAULT_UNIT_TOKENS`
and `UNIT_SUFFIX_TOKENS` were both audited: no other semantic token is classified as a unit.

---

## 6. Total mud volume — cross-source analysis

Traced end to end through the live corpus (not inferred from names):

| Source | Section | Header / wording | Extracted field | Predicate (before) | Predicate (after) |
|---|---|---|---|---|---|
| `daily_drilling_report_well-a3.docx` | prose | `1,450 bbl total system volume` | `mud_volume_bbl` | `mud_volume` | `mud_volume` |
| `mud_report_well-a3.xlsx` | prose | `Total mud volume (bbl) 1450 bbl` | `mud_volume_bbl` | `mud_volume` | `mud_volume` |
| `mud_report_well-a3.xlsx` | summary cell | `total_mud_volume` = 1450 | `total_mud_volume_bbl` | **`total_mud_volume_bbl`** | `mud_volume` |

**Case A: they are one quantity.** The evidence is in the repository, not in a guess:

* `mud.SUMMARY_ALIASES` already maps `"total mud volume"`, `"mud volume"` and `"active system volume"`
  onto **one** property, `total_mud_volume`;
* the golden mud report states the same figure twice in one file — the prose line and the summary
  cell, both 1450;
* the daily report states `1,450 bbl total system volume` for the same well.

Three statements of one number, and the contract already said they were one label. So
`total_mud_volume` / `total_mud_volume_bbl` are now aliases of `mud_volume`. Sharing the unit `bbl`
was **not** the reason and would not have been sufficient.

`pill_volume` and `kick_volume` remain separate, and `test_a_pill_volume_beside_a_system_volume_is_not_a_conflict`
proves the unification did not unify two different quantities.

### The conflict test (§16)

Two sources, different values, no filename used as context:

```
source_a.txt   "Total system volume: 1450 bbl"
source_b.txt   "Total mud volume: 1500 bbl"
```

Result: **exactly one conflict**, `mud_volume`, candidates `{"1450 bbl", "1500 bbl"}`. Before the
alias was registered these arrived under two predicates and could never be compared, so this real
disagreement was **invisible**. That is the previously-hidden conflict §31 predicted, and it is now
exposed.

---

## 7. Conflicts before and after

Measured by running the 14-file V4 forensic corpus through ingest → promote → `detect_conflicts`.

| | before (V4.2, `c279d1c`) | after (V4.3) |
|---|---|---|
| open conflicts | **2** | **2** |
| properties | `hole_section_size`, `measured_depth` | `hole_section_size`, `measured_depth` |

* `false_conflicts_removed`: **none this mission** — V4.2 already removed the three semantic false
  conflicts (`surface_pressure`, `mud_volume`, `rpm`) and de-contaminated `hole_depth`.
* `newly_exposed_real_conflicts` on the standing corpus: **none**. The newly-exposable class is the
  total-mud-volume disagreement of §6, which the standing corpus does not contain because all three
  of its statements agree on 1450.
* `ambiguous_cases`: the conflict detector reports **7** single-source ambiguities on this corpus,
  counted separately from conflicts and never folded into either category.

The count did not fall, so nothing was merged to make it smaller. It did not rise on this corpus
because the corpus has no total-mud-volume disagreement.

---

## 8. Exact limitations

Stated plainly.

1. **Recovery depends on a recorded excerpt.** An entry stored with no `provenance.excerpt` is
   classified `requires_reextraction` and left alone. On the current V4 corpus that count is **0**,
   but the category exists and is reported rather than silently kept.
2. **Two corpus rows stay ambiguous.** `hole_size` / `"8.5"` and `total_mud_volume_bbl` / `"1450"` are
   bare Excel cells whose excerpt carries no label. Their predicates *are* in the split table, so
   they are reported as ambiguous and left exactly as they are. Re-extraction from the source
   workbook is the only way to settle them, and the table extractor's header context — not the cell —
   is where that information lives.
3. **The qualifier vocabulary is two tokens.** `md` and `tvd` only. `OD`, `ID`, `ROP`, `ECD`, `MW`,
   `PV`, `YP`, `GEL` were audited: none is currently lost, because none is used as a parenthesised
   qualifier by any contract in this repository. Adding one without a contract that consumes it
   would be dead vocabulary.
4. **The repair matrix is a closed table by design.** A newly split predicate is not repairable until
   a row is added. That is a deliberate cost: an open-ended rule is what produced the
   `mud_balance → date_iso` demotion.
5. **`doctor` exits 1.** Two genuine disagreements remain open. Not made green.
6. **No production workspace was repaired.** The mechanism is proven on fixtures; §10 is the
   procedure, and it is dry-run first by design.

---

## 9. Documentation integrity

The V4.1 self-reference defect was already corrected in V4.2 and remains corrected.

**New finding:** `test_report_integrity.py` failed in this workspace because the clone is shallow
(7 reachable objects), so `b7703baf`, `921f89be` and `b2e76fad` are legitimately absent. The test
could not tell "the report named a SHA that never existed" from "this checkout never fetched it".
Fixed: under shallowness it **skips with the missing SHAs named**, and the strict assertion still runs
on a complete checkout. Skipping is the honest result; passing would have been a lie. Now
**2 passed, 2 skipped**.

All SHA claims in `docs/` were re-enumerated; every one inside the shallow window resolves.

---

## 10. Production repair procedure

No production-wide repair is executed automatically. The controlled sequence:

```
1. discover workspaces
2. dry run            knowledge rebuild --dry-run  (classification only; writes nothing)
3. report             scanned / deterministic / ambiguous / requires_reextraction,
                      per row: document, version, field, old predicate, new predicate, reason
4. review             a human reads the deterministic list and the ambiguous list
5. apply              knowledge rebuild          (re-derives from stored artefacts; atomic)
6. reindex            index rebuild              (the index is disposable and rebuilt wholesale)
7. verify conflicts   knowledge conflicts        (false ones gone, real ones still open)
8. verify integrity   doctor                     (still nonzero if real disputes remain)
```

Properties that make this safe:

* the dry run is **read-only** — `test_the_stored_versions_are_untouched_by_classification` asserts
  every `DocumentVersion.sha256` is unchanged;
* applying is a **rebuild**, which is already transactional and already removes only `EXTRACTED`
  rows, so a note a person typed survives;
* it is **idempotent** — `test_the_repair_is_idempotent` runs it twice and compares predicate counts,
  every `(predicate, value, lookup_key)` row and the conflict count; all identical;
* nothing is **silently skipped** — every row lands in one of the four reported categories.

A `--dry-run` flag on the CLI is the remaining piece of wiring; the classifier it would call
(`plan_recovery`) is implemented, tested and already returns exactly the report shape step 3 needs.

---

## 11. Search, retrieval and provenance

* Search indexes every fact including `CONFLICTED` ones; both sides of the `measured_depth`
  disagreement remain findable after a rebuild (`test_both_sides_of_a_real_disagreement_stay_retrievable`).
* No synonym or abbreviation expansion was added — that would be inference in a deterministic
  retrieval path.
* After a repair, every separated quantity still carries `document_version_id`, a `lookup_key`
  containing `property:<predicate>`, `provenance[0].document_id` and `source_sha256`.
* `record_state` and supersession are unchanged by the repair: the sorted
  `(record_state, predicate, value)` triples of the whole corpus are identical before and after two
  rebuilds.

---

## 11a. `doctor`, observed verbatim

```
exit code: 1
knowledge: {"by_status": {"ACTIVE": 59, "CONFLICTED": 11, "UNVERIFIED": 10},
            "facts": 80, "open_conflicts": 2}
findings:
  - "2 unresolved knowledge conflict(s): `drillintel knowledge conflicts`"
  - "the search index disagrees with the registry: `drillintel index rebuild`"
  - "18 structured row(s) not yet indexed, 0 no longer searchable, 0 orphaned: `drillintel index rebuild`"
```

The three findings are three different things and are not overloaded onto one status: a **real
conflict** (2), a **disposable index that is behind the registry**, and **structured rows awaiting
indexing**. Neither of the latter two is a semantic defect and neither is counted as a conflict.

`doctor` was **not** made green. Its exit code is 1 because two genuine disagreements remain open.

Knowledge items on the V4 corpus moved 81 → **80** and `UNVERIFIED` 11 → **10**, for the same reason
as the CLI corpus in §14: the mud workbook's total volume was recorded twice, once as
`mud_volume_bbl` (paragraph, ACTIVE) and once as `total_mud_volume_bbl` (summary cell, UNVERIFIED).
Unified, that is one corroborated row instead of an ACTIVE row beside an UNVERIFIED shadow of itself.

---

## 12. Performance

Dry run over the whole V4 corpus (85 stored fields): **0.0021 s**. Classification is a pure function
over in-memory payloads — there is no N+1 to find, and `test_the_dry_run_is_linear_and_makes_no_database_queries`
runs 500 payloads to hold that.

Repair adds one `scan_text` per stored entry during a rebuild, which already parses every artefact.

---

## 13. Verification actually run

| Check | Command | Result |
|---|---|---|
| V4.3 suite | `pytest tests/integration/test_knowledge_semantic_repair_v43.py` | **74 passed** |
| V4.2 suite | `pytest tests/integration/test_knowledge_semantic_vocabulary_v42.py` | **24 passed** |
| Conflict semantics | `pytest tests/integration/test_knowledge_conflict_semantics_v4.py` | **6 passed** |
| Report integrity | `pytest tests/unit/test_report_integrity.py` | **2 passed, 2 skipped** (shallow clone) |
| CLI | `pytest tests/unit/test_cli.py` | **34 passed** |
| Full suite | `pytest -p no:cacheprovider -o addopts="--strict-markers"` | see §14 |
| Lint | `ruff check src tests` | clean |
| Compile | `python -m compileall -q src` | clean |
| Git | `git status --short` after commit | clean |

### Mutation proofs

| Mutation | Result |
|---|---|
| `SEMANTIC_QUALIFIERS` emptied | `Depth (ft MD)` and `Depth (ft TVD)` both collapse to `depth` — indistinguishable |
| `rheometer_speed` folded onto `rpm` | phantom `rpm` conflict returns |
| `true_vertical_depth` folded onto `measured_depth` | phantom `measured_depth` conflict returns |
| `sidpp`/`sicp`/`maasp` folded onto `surface_pressure` | phantom `surface_pressure` conflict returns |
| recovery disabled | frozen artefact rebuilds back to `hole_depth`/`surface_pressure` and stays there |
| recovery matching any label (the pre-fix version) | `mud_balance → date_iso` demotion returns |

---

## 14. Test count reconciliation

| | collected | passed | failed | skipped | duration |
|---|---|---|---|---|---|
| V4.2 baseline (`c279d1c`) | 1341 | 1338 | 0 | 3 | 730.83 s |
| V4.3 | **1415** | **1410** | **0** | **5** | 855.79 s |

The arithmetic closes exactly: +74 tests, all of them
`tests/integration/test_knowledge_semantic_repair_v43.py`; and 2 report-integrity tests moved from
passed to skipped because this clone is shallow (§9). No test was deleted, skipped to hide a failure,
or weakened.

Four `tests/unit/test_cli.py::TestKnowledgeCommands` tests pin exact corpus counts and failed at
62 → 61. They were **not** retyped first: the delta was traced row by row (§11a, §6) to the merged
total-mud-volume duplicate, and only then were the constants changed, with the reason recorded inline
in the test.

---

## 15. Cleanup

* No ad-hoc migration script was added — the repair is a derivation, not a one-time script, so there
  is nothing to leave behind in production code.
* No alias was added speculatively; `total_mud_volume` is the only new alias and §6 is its evidence.
* `SEMANTIC_QUALIFIERS` is two tokens with a docstring explaining why it is not larger.
* The V4.2 `UNREGISTERED_FIELDS` list was updated to drop `total_mud_volume_bbl`, with the reason
  recorded inline rather than the entry silently deleted.
* No branch, code or document was deleted. `git worktree` used for the V4.2 comparison was removed.
