# V4.4 — Operational Recovery, Knowledge Dry-Run, Reconciliation & Production Certification

**Status: CERTIFIABLE** — with the limitations in §12 stated, not buried.

Every figure below was produced by a command run against this checkout during this mission. Where a
previous report is contradicted, the contradiction is named in §2 rather than quietly overwritten.

---

## 1. Git topology, established from objects and not from reports

| Question | Command | Result |
| --- | --- | --- |
| Branch | `git branch --show-current` | `arena/01a0c936-prog-proc` |
| Local HEAD at mission start | `git rev-parse HEAD` | `e8621136ca73108ae7b590e6baa72fedc1f00835` (grafted) |
| Remote tip at mission start | `git ls-remote origin refs/heads/arena/01a0c936-prog-proc` | `c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9` |
| Shallow? | `git rev-parse --is-shallow-repository` | `true` |

### The V4.3 commit does not exist

The V4.3 report claimed a local commit `4ab2c452` (and its predecessor `45b4ec24`). **Neither object
exists.** Verified with the check that actually looks the object up, not `rev-parse --verify`,
which succeeds on any well-formed hash:

```text
$ git cat-file -e 4ab2c4529ef84ce6248f36af3127d0bfc13d6d8a^{commit}   # -> non-zero, ABSENT
$ git cat-file -e 45b4ec24b9ffba0b1ee3ca4705af2c2c4b95f11e^{commit}   # -> non-zero, ABSENT
$ git cat-file -e c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9^{commit}   # -> 0, PRESENT (after fetch)
```

`c279d1c` was not present either until `git fetch origin arena/01a0c936-prog-proc` (the default
refspec here is `+refs/heads/main`, so a branch must be fetched by name). The safety tag, the stash
and the `/tmp` backup the V4.3 session recorded were all gone as well; `.venv` had to be
re-provisioned.

So the relationship `c279d1c → 4ab2c452` **cannot be preserved, because one end of it was never an
object anyone can reach.** It is not rewritten; it is reported as unavailable. What survived was the
*file content*, in the working tree.

### What was in the working tree, measured rather than assumed

The tree held 30 modified files, 1 deletion and 26 untracked files against HEAD `e862113`. Because
those files are untracked at `e862113`, `git diff c279d1c` masked their real content behind `D`
rows, so each was hash-compared against its `c279d1c` blob individually:

```text
23 files present in c279d1c :  18 byte-identical, 5 differ
 5 that differ : docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md
                 src/drilling_intelligence/operations/survey.py
                 src/drilling_intelligence/operations/tableshape.py
                 tests/integration/test_knowledge_semantic_vocabulary_v42.py
                 tests/unit/test_report_integrity.py
 4 modified tracked files   : knowledge/facts.py, knowledge/service.py,
                              operations/mud.py, tests/unit/test_cli.py
 3 new files                : knowledge/recovery.py,
                              tests/integration/test_knowledge_semantic_repair_v43.py,
                              docs/ARENA_V4_3_RETROACTIVE_SEMANTIC_REPAIR_LAST_RESULT.md
```

That is exactly a V4.3 delta and nothing else: 12 files. Nothing was lost.

### Recovery, without destroying anything

`e862113` was proven an ancestor of `c279d1c` (`git merge-base --is-ancestor`, exit 0), the 12-file
delta was backed up, the tree was stashed with `git stash push -u`, fast-forwarded with
`git merge --ff-only c279d1c`, and the delta restored — all 12 re-hashed identical. No `reset`,
no rebase, no force-push, no deletion. The stash remains as a safety net.

The delta was then committed as a **re-land**, `9ecb64dfef7d7a2c7e72cf11a2bd63dd8b8e2216`, whose
message states plainly that the original object never reached the remote and is not present, rather
than pretending otherwise. Parentage verified:

```text
$ git rev-parse HEAD^   -> c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9
```

History therefore reads `c279d1c → 9ecb64d → <V4.4 work>`, and was pushed and verified twice:

```text
$ git ls-remote origin refs/heads/arena/01a0c936-prog-proc
9ecb64dfef7d7a2c7e72cf11a2bd63dd8b8e2216   (before the V4.4 commits)
$ gh api repos/asgareyvazi/Prog-Proc/branches/arena/01a0c936-prog-proc --jq .commit.sha
9ecb64dfef7d7a2c7e72cf11a2bd63dd8b8e2216
```

---

## 2. Corrections to prior reports

1. **V4.3's "committed `4ab2c452`, push failed on an expired token" was half right.** The push did
   fail, and the commit did exist at the time — but the object is gone now. Reporting it as a
   recoverable local commit would have been false this mission.
2. **`KnowledgeFact.lookup_key` is a plain method at `facts.py:728`, not a `@property` at line 716.**
   Verified by `isinstance(KnowledgeFact.lookup_key, property)` → `False` and by reading the
   definition. Its *semantics* are as documented (subject + predicate + record state; values
   excluded), only the form and line were wrong.
3. **A real defect was found that no prior report mentions:** `rebuild --well` was a data-loss
   command. See §6.

---

## 3. V4.2 semantic contract, re-verified by import

```text
PREDICATES                  = 34
PREDICATE_BY_FIELD          = 91
"md" in UNIT_SUFFIX_TOKENS  = False      "tvd" in UNIT_SUFFIX_TOKENS = False
UNIT_SUFFIX_TOKENS          = 19
sidpp / sicp / maasp / measured_depth / true_vertical_depth / rheometer_speed /
kick_volume / pill_volume / mud_volume / bit_size / hole_section_size  -> all REGISTERED
total_mud_volume            -> mud_volume
total_mud_volume_bbl        -> mud_volume
tableshape.SEMANTIC_QUALIFIERS = ("md", "tvd")
recovery.SPLIT_PREDICATES keys = hole_depth, hole_section_size, mud_volume, rpm, surface_pressure
```

Nothing was weakened to make V4.4 easier. All 171 tests across the V4.2/V4.3 suites
(`test_knowledge_semantic_repair_v43`, `test_knowledge_semantic_vocabulary_v42`, `test_cli`,
`test_report_integrity`, `test_tableshape`, `test_mud_unit_matrix_v4`,
`test_knowledge_conflict_semantics_v4`, `test_knowledge_pipeline`, `test_knowledge_facts`) passed
unchanged before any V4.4 code was written.

---

## 4. `plan_recovery` before this mission — answered from code

| Question | Answer |
| --- | --- |
| Inputs | `Iterable[Mapping]` of stored artefact payloads (`Extraction.document_json`) |
| States classified | `unchanged`, `deterministic`, `ambiguous`, `requires_reextraction` — **per stored field** |
| Distinguishes workspace states? | **No.** It never looked at workspace state at all |
| Recommends a command? | No |
| Inspects the database? | No — pure function |
| Deterministic? | Yes, keyed to the closed `SPLIT_PREDICATES` table |
| Invocable without mutating? | Yes |
| Exposed by the CLI? | **No** — `grep -c plan_recovery src/drilling_intelligence/cli/app.py` returned `0` |
| Exposed in JSON? | No |
| Explains *why*? | No output at all |

The signals for a workspace-level answer already existed in `doctor`, `status()` and
`SearchService.stats()`.  Rather than duplicating them, `assess_recovery()` was added to
`recovery.py` as a pure function over those numbers, and `plan_rebuild()` in the service wires it to
the real database. No CLI-specific conditionals were introduced.

---

## 5. `knowledge rebuild --dry-run`

```bash
drillintel knowledge rebuild --dry-run
drillintel knowledge rebuild --dry-run --json
drillintel knowledge rebuild --dry-run --well A-3
```

**One planning path.** `plan_rebuild()` calls the same `rebuild()` the real command calls, inside a
transaction that is rolled back, and reports what it actually did. See
`docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md` §20 for why the rollback is sufficient and what the three
guards are.

### Dry-run prediction vs. real execution (V4 forensic corpus)

| Quantity | Predicted | Actual |
| --- | --- | --- |
| remove | 80 | 80 |
| create | 80 | 80 |
| update | 5 | 5 |
| unchanged | 0 | 0 |
| relations | 85 | 85 |
| conflicts | 2 | 2 |

Every number agrees because there is only one arithmetic.

### Side-effect proof

`knowledge_item`, `knowledge_relation`, `knowledge_conflict`, `document_version`, `extraction` and
`document` are each rendered row-by-row (timestamps included) and hashed, alongside the SHA-256 of
the registry and index SQLite files. Before and after a dry run: **identical**. Counting rows would
have passed a command that merely bumped `updated_at`.

### Mutation proofs

| Mutation | Test that caught it | Observed failure |
| --- | --- | --- |
| Drop the `well_id` filter from `delete_derived` | `test_a_scoped_rebuild_leaves_the_other_well_alone` | `assert 61 < 61` — the dry run planned to remove another well's rows |
| `session.commit()` instead of the final `session.rollback()` | `test_the_dry_run_touches_nothing` | `knowledge_item.sha256`, `knowledge_relation.sha256`, `knowledge_conflict.sha256` and `database_path.sha256` all differ |
| Report `create` as an independent estimate | `test_the_dry_run_predicts_what_the_real_rebuild_actually_does` | `assert 61 == 6` |

All three source files were restored from a `/tmp` copy and hash-verified identical afterwards.

---

## 6. A defect found and fixed: scoped rebuild was a data-loss command

`KnowledgeExtractionService.rebuild()` passed `well_id` to `sync_all()` but **not** to
`delete_derived()`, which had no such parameter. Measured on the generated corpus, with one document
re-filed to a second well:

```text
before rebuild --well A-3 : total facts 61
after  rebuild --well A-3 : total facts 45   (exit 0, no warning)
```

**16 derived rows destroyed and reported as a successful run.** `delete_derived` now takes `well_id`
and the deletion is scoped exactly as the re-derivation is. The dry run plans the narrow deletion
too, so the operator sees the true blast radius before executing.

### A second finding, surfaced by the dry run rather than by reading the code

Running the finished command over the V4 forensic corpus printed a plan of zeros. Tracing it:
`IngestionPipeline.run()` without a workspace id files documents under `workspace_id IS NULL`, so
the CLI's `workspace_id=_workspace_row_id(workspace)` filter matches no version. The **real**
command has always behaved this way — verified against the V4.3 re-land commit itself:

```text
$ drillintel knowledge rebuild --json     # on that corpus, at commit 9ecb64d
exit 0   versions: 0   removed: 0   facts: {created: 0, updated: 0, unchanged: 0}
```

A silent no-op reported as success. That is pre-existing and is **not** changed here — narrowing it
would alter ingest/rebuild behaviour well beyond this mission's scope. What changed is that the dry
run now names the mismatch instead of printing zeros an operator would have to interpret:

```text
warning: this scope matched no current document version, so nothing would be re-derived, yet 80
derived row(s) are reported as detached; a corpus ingested without a workspace id is invisible to a
workspace-scoped rebuild
```

`test_a_scope_that_matches_nothing_says_so` pins it, and asserts alongside that the same plan with
no workspace filter covers the corpus (`versions > 0`), so the warning is about scope and not about
the classifier.

---

## 7. Idempotency, in the accounting the repository actually uses

Measured on the generated corpus:

| Operation | created | updated | unchanged | removed |
| --- | --- | --- | --- | --- |
| `rebuild` (1st) | 61 | 5 | 0 | 61 |
| `rebuild` (2nd) | 61 | 5 | 0 | 61 |
| `sync_all` (3rd, no delete) | 0 | 16 updated | 50 | — |
| `sync_all` (4th) | 0 | 16 updated | 50 | — |

`rebuild` deletes derived rows first by design, so `created` counts re-derivation rather than new
information; asserting `created == 0` there would be asserting the command is something it is not.
The invariant that holds — and is tested — is that **the resulting fact set is identical**
(predicate, value and open-conflict counts compared), and that `sync_all` invents nothing
(`created == 0`) with a split that is stable rather than oscillating.

Two consecutive dry runs return byte-identical plans.

---

## 8. Semantic repair through the dry run

V4 forensic corpus (85 stored fields):

```text
deterministic                   0
requires_context                2
requires_source_reextraction    0
no_change                      83
```

The two `requires_context` rows are bare spreadsheet cells whose recorded excerpt is the value
itself, so there is no label to read — `hole_size` beside `"8.5"` and `total_mud_volume_bbl` beside
`"1450"`. Both keep their stored name. `deterministic = 0` is a **result**: this corpus is already
correct.

The classifier's ability to find repairable cases is proven separately on the frozen pre-V4.2
fixture, where `COLLAPSED_NAMES` rewrites stored field names back to their pre-fix spellings while
leaving `provenance.excerpt` untouched. `test_the_dry_run_classifies_a_frozen_pre_fix_artefact`
asserts `deterministic > 0` there, that every deterministic row has a non-empty excerpt, and that
the recovered field is one the collapse table actually maps to the stored name. A repair without a
recorded excerpt would be a guess, and the test forbids it.

---

## 9. Conflicts, index, structured rows

```text
doctor, promoted V4 forensic corpus, before anything runs:
  exit 1
  - 2 unresolved knowledge conflict(s): `drillintel knowledge conflicts`
  - the search index disagrees with the registry: `drillintel index rebuild`
  - 18 structured row(s) not yet indexed, 0 no longer searchable, 0 orphaned: `drillintel index rebuild`

  knowledge = {"facts": 80, "open_conflicts": 2,
               "by_status": {"ACTIVE": 59, "CONFLICTED": 11, "UNVERIFIED": 10}}
```

After `knowledge rebuild` alone: `missing_versions` 14 → 0, `knowledge_chunks` 0 → 80,
`structured_missing` **still 18**. After `index rebuild` too: `structured_missing` 0,
`structured_records` 18. **Exit code stays 1 throughout**, because the remaining finding is the two
genuine engineering conflicts.

The 18 rows were genuinely missing, not a deliberate exclusion: `structured_searchable_ids()`
returned 18 while the index held 0, and `structured_row_ids() - structured_searchable_ids()` was 0.
Child tables are deliberately excluded from the vocabulary and folded into their parent's unit
(`tests/unit/test_structured_index_boundary.py`), but none of them are counted in
`structured_missing`, so the counter measures real drift. The doctor wording is accurate. No counter
was changed to obtain a zero.

A knowledge rebuild **does not** resolve conflicts: `before 2`, `predicted 2`, and the real rebuild
reported 2. Ambiguity within one source stayed at 7.

---

## 10. Production corpus audit (Phase 23)

V4 forensic corpus, before → after a real rebuild. **Every unchanged count is unchanged; only the
index counters moved.**

```text
documents 14   versions 14   artefacts 14   facts 80   relations 80
by_origin {EXTRACTED: 80}    by_status {ACTIVE 59, CONFLICTED 11, UNVERIFIED 10}
open_conflicts 2   detached_facts 0   needs_rebuild False   manual_facts 0
predicates: sidpp 1, sicp 1, maasp 2, measured_depth 11, true_vertical_depth 1,
            rheometer_speed 1, kick_volume 1, mud_volume 2, hole_section_size 4, rpm 2, ...

changed by the rebuild:
  index_knowledge_chunks  0 -> 80
  index_chunks            0 -> 253
  index_missing_versions 14 -> 0
nothing else.
```

Separated predicates are all present and distinct, which is the V4.2/V4.3 contract holding under a
real rebuild.

---

## 11. Verification

| Gate | Command | Result |
| --- | --- | --- |
| Full suite | `pytest` | **1449 passed, 0 failed, 5 skipped** (1454 collected; exit 0) |
| New tests | `tests/integration/test_knowledge_rebuild_dry_run_v44.py` (20), `tests/unit/test_recovery_state_v44.py` (18) | 38 |
| Lint | `ruff check src tests` | clean |
| Format | `ruff format` | clean |
| Compile | `python -m compileall src tests` | clean |
| Whitespace | `git diff --check` | clean |
| Migration | `alembic heads` | `0011 (head)` — **no schema change, so no migration added** |
| Docs integrity | `pytest tests/unit/test_report_integrity.py` | 3 passed, 2 skipped (shallow boundary) |

The 5 skips are 3 PySide6 UI tests and 2 report-integrity tests that skip on a shallow clone.

### Performance (seconds, V4 forensic corpus)

```text
ingest          0.7754
plan_rebuild    0.4349   <- the dry run is *cheaper* than the real rebuild
real_rebuild    0.4850
index_rebuild   0.2267
```

No N+1 queries were introduced: the plan reads artefacts once per in-scope version through
`extraction_for_version`, and `plan_recovery` remains a pure function over the payloads.

### Documentation-integrity hardening

`test_report_integrity.py` skipped wholesale on a shallow clone, which would also have excused a
SHA for a commit that never existed — precisely the `4ab2c452` case. A new test now allows only the
three genuinely pre-boundary commits (`b7703baf…`, `921f89be…`, `b2e76fad…`); any other absent
40-hex SHA fails, shallow clone or not. This report therefore cites `4ab2c452` in short form and
says in prose that the object is unavailable, rather than dressing it up as a tree anyone can check
out.

---

## 12. Known limitations

1. **The dry run executes the real rebuild inside a rolled-back transaction.** It is therefore as
   expensive as a rebuild, not cheaper by an order of magnitude (0.435 s vs 0.485 s here). On a very
   large workspace that cost is real, and it is the price of a preview that cannot drift.
2. **`plan_rebuild` reports workspace-level `status()` counters even for a `--well` run**, because
   `status()` has no well parameter. The scope block and the plan's own counts are well-scoped; the
   `recovery` assessment is not.
2a. **A corpus ingested without a workspace id is invisible to a workspace-scoped rebuild.** This is
   pre-existing (`IngestionPipeline.run()` leaves `document.workspace_id` NULL; the CLI has always
   filtered on a real id) and is now *reported* rather than fixed. See §6.
3. **The real `rebuild` does not refuse on an `unrecoverable` state.** Only the dry run returns
   exit 3. `doctor` owns integrity reporting; changing the executor's contract was out of scope and
   is documented rather than hidden.
4. **`4ab2c452` is unrecoverable.** The V4.3 work is present as content and re-landed as `9ecb64d`,
   but the original commit object does not exist and no SHA will ever resolve to it.
5. **The qualifier vocabulary is two tokens** (`md`, `tvd`). OD/ID/ROP/ECD/MW/PV/YP/GEL were audited
   and none is currently lost, but a new qualifier must be declared, not inferred.
6. **The repair matrix is closed by design.** `SPLIT_PREDICATES` gains a row only by pointing at the
   vocabulary fix that made it; recovery may refine a name and never demote one.
7. **`doctor` exits 1 on the standing corpus and will continue to.** Two genuine engineering
   conflicts remain open, which is the correct answer.
8. **No production workspace was repaired.** The procedure is dry-run first, and nothing here
   executes recovery automatically on the strength of a plan.

---

## 13. Commits

| Role | SHA |
| --- | --- |
| V3 baseline (pre-boundary, absent in this shallow clone) | `b7703baf35abd84881432a93264a979c33751c6c` |
| Local HEAD at mission start (grafted) | `e8621136ca73108ae7b590e6baa72fedc1f00835` |
| Verified remote tip at mission start | `c279d1c61b7301bfb5d54d6f8c285ee2186bcfd9` |
| V4.3 re-land (parent `c279d1c…`) | `9ecb64dfef7d7a2c7e72cf11a2bd63dd8b8e2216` |
| V4.4 work | see `git log` — a document cannot name the commit that carries it |
