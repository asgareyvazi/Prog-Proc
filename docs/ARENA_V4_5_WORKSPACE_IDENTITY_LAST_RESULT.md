# V4.5 — Workspace Identity, Scope Integrity and the Knowledge Boundary

Branch `arena/01a0c936-prog-proc`, on top of the V4.4 re-land `c333dca2806f778c0260ee193f67524e6b1cc8da`.

The question this mission had to answer: **if the system says "this workspace contains these
documents", does every downstream subsystem see exactly that population?** Before V4.5 it did not,
and the failure was silent — a command exited 0 while reporting zero work over a workspace that was
holding fourteen documents.

---

## 1. The defect, reproduced before it was fixed

`Document.workspace_id` (`database/models.py:241`) is a **nullable** `ForeignKey("workspace.id",
ondelete="SET NULL")` and part of `UniqueConstraint("workspace_id", "identity_path")`.

`IngestionPipeline.run()` accepted `workspace_id: str | None = None` and simply forwarded whatever it
was given to `PipelineResult`, `IngestionRun`, `planner.plan`, `registry.register` and
`subject_id=workspace_id or "default"`. It never resolved one itself.

Exactly one caller supplied it: `cli/app.py`, in a function called `_workspace_row_id`. Every other
caller — the desktop UI, the API, every test fixture — left it NULL. So a workspace row for the
folder existed in the registry, the documents existed in the registry, and the column that joins
them was empty.

Measured on the V4 forensic corpus with a script run before any code was changed:

| signal | before the fix |
| --- | --- |
| workspace registry rows for the folder | 1 |
| documents | 9 |
| `distinct document.workspace_id` | `{"None"}` |
| documents with a well id | 9 |
| `knowledge rebuild --dry-run` exit code | **0** |
| …reported `plan.versions` | **0** |
| …`recovery.state` | `knowledge_stale` |

A workspace-scoped rebuild filtered on `Document.workspace_id`, matched nothing, re-derived nothing,
and told the operator it had succeeded. The identity was not missing from the system — it was
missing from the rows.

## 2. Where the fix goes, and why there

Not in the CLI: that fixes one caller of several. Not in `rebuild()`: that is downstream of the
write. Not as an `OR workspace_id IS NULL` fallback in the queries: that would redefine a state that
already has a meaning, because `ondelete="SET NULL"` makes NULL mean "the owning workspace row was
deleted". Blanket-converting NULLs would destroy that distinction.

The fix is at the **persistence boundary**, which is the only place that knows the folder:

- **`WellRepository.resolve_workspace_id(root, *, name="", data_dir="")`** (`wells/repository.py`) is
  the one authoritative answer to "which workspace row owns this folder". It resolves each stored
  `root_path` and compares resolved paths — `Workspace.root_path` is `unique`, so this is a lookup
  and not a guess — then calls the existing `get_or_create_workspace`.
- **`IngestionPipeline.workspace_identity()`** caches that answer for the run, and `run()` calls it
  whenever the caller did not supply an id. One extra query per run, not per document.
- **`cli/app.py::_workspace_row_id`** now delegates to the same function instead of carrying its own
  copy, so the CLI and the pipeline cannot disagree about which row owns a folder.

An explicit `workspace_id` argument still wins; it is an override, not a suggestion.

After the fix, same script, same corpus:

| signal | after the fix |
| --- | --- |
| workspace registry rows | 1 (the existing row, reused — no duplicate) |
| documents with a NULL workspace | **0** (was 9) |
| `distinct document.workspace_id` | one id |
| dry-run exit / `plan.versions` / `remove` | 0 / **9** / **76** |
| `recovery.state` | `conflicts_present` |

The recovery state changed because it is now computed over documents that exist in scope rather than
over none. That is the point of the mission: the state machine was never wrong, it was being fed an
empty population.

## 3. Scope: three filters that had to become one

`rebuild` deletes derived rows and then re-derives them; `status` reports on the result. Those three
operations each carried their own copy of the population predicate, and V4.4 had already proved they
could drift — `delete_derived` was workspace-wide while `sync_all` was well-scoped, and a
`rebuild --well A-3` silently destroyed 16 rows.

**`KnowledgeScope(workspace_id, well_id)`** (`knowledge/repository.py`) now defines the population
once. It is a frozen dataclass with a single `document_ids()` subquery, and `delete_derived`,
`counts`, `_current_version_pairs` and `_staleness` all build their filters from it. Small and
immutable on purpose: this is a filter, not a framework. Both fields are optional, because a
document belongs to a workspace without belonging to a well, and an empty scope legitimately means
"the whole file" — a workspace is a file.

Two structures are **global by construction** and are documented as such rather than half-scoped:
`knowledge_relation` carries no scope column at all (edges connect ids), and `knowledge_conflict`
carries a well but no workspace, so a well narrows the conflict count and a workspace does not.
`counts()`' docstring now says which is which.

`status(workspace_id, well_id)` gained the `well_id` parameter, and `plan_rebuild` now reads its
`status` with the *same* scope it executes with. Previously the planner described the whole workspace
while rebuilding one well.

## 4. Zero: legitimate, or a defect

A plan of zeros has three causes and only one is a defect. The distinction is made from rows in the
document table, not from a threshold. Verified by running all three:

| situation | evidence | what the operator is told |
| --- | --- | --- |
| registry is empty | `documents_total == 0` | nothing — a legitimate zero, and warning would be crying wolf |
| workspace owns documents, none derivable | `documents_in_workspace > 0` | "nothing is derivable from them yet — a processing gap rather than an identity one" |
| scope matches nothing, registry is not empty | `documents_total > 0` | "the workspace id is wrong, or documents were ingested before identity was resolved at the pipeline and carry no `workspace_id`" |

The third message replaces V4.4's, which guessed at this from `detached_facts`. Once `_staleness`
was scoped properly that counter went to zero in exactly the case the warning was written for, so the
evidence had to move to the registry.

## 5. Identity in `doctor`

`DocumentRepository.counts()` now returns `unscoped_documents`, and `doctor` renders it as its own
`identity` note. Deliberately **not** a finding: the column is nullable by design, so an orphan whose
workspace row was deleted is a legitimate state, and a doctor that fails on a state the schema
permits is a doctor nobody runs. Findings change the exit code; notes do not.

```
identity   healthy - every document names its workspace
identity   6 document(s) with no workspace id: invisible to every workspace-scoped query; either
           orphans of a deleted workspace row or ingested before identity was resolved at the pipeline
```

## 6. Legacy NULLs

No migration, and no backfill tool. Reasoning, in order:

1. Fresh ingestion no longer produces NULLs (proved above).
2. A NULL is reachable only through `ondelete="SET NULL"`, i.e. a deleted workspace row, or through
   data ingested before this fix.
3. For the first kind, ownership is **not** deterministic — the row that would say which workspace
   owned the document is gone. Reconstructing it from the current folder would be a guess dressed as
   a repair.
4. Adding `NOT NULL` would be wrong for the same reason: the schema's own cascade produces NULLs on
   purpose.

So the honest classification of a NULL is `orphaned_workspace_deleted`, and `doctor` counts them.
`tests/integration/test_workspace_identity_v45.py::test_null_workspace_rows_are_classified_not_blindly_filled`
deletes a workspace row, confirms the cascade produces NULLs, and asserts nothing overwrites them.
A backfill becomes worth writing only if a real deployment shows legacy NULLs whose owner is
recoverable from evidence; there is no such evidence in this repository.

## 7. Verification

### Real-corpus reconciliation (V4 forensic corpus, 14 documents)

| check | result |
| --- | --- |
| documents total / with workspace / without | 14 / **14** / **0** |
| documents with a well / without | 14 / 0 |
| workspace registry rows | 1 (before and after) |
| `plan_rebuild` predicted vs executed | remove **80/80**, create **80/80**, update **5/5** |
| document rows changed by the rebuild | **none** (identity paths, workspace ids, well ids identical) |
| document versions | 14 → 14 |
| knowledge rows | 80 → 80, **0 new ids, 0 removed ids** |
| lookup keys for surviving rows | unchanged |
| index | chunks 0 → 253, knowledge chunks 0 → 80, documents 0 → 14, versions 0 → 14 |
| recovery state | `conflicts_present` before and after (2 real conflicts, correctly not "corrupt") |
| timings | ingest 0.7667 s, plan 0.4156 s, rebuild 0.4578 s |

The 2 conflicts are `hole_section_size` and `measured_depth`, both genuine and both left unresolved —
`doctor` still exits 1. Nothing here was weakened to make a number green.

### Mutation proofs

Each mutation is a real change to the shipping code, applied, run, and reverted from a `/tmp` copy:

| mutation | expectation | result |
| --- | --- | --- |
| **D** — remove the identity resolution from `IngestionPipeline.run()` | identity tests fail | **caught**, 7 tests fail |
| **E** — `_staleness` ignores `well_id` | well-scoped staleness leaks the workspace total | **caught**, `assert 6 == 2` |
| **F** — file the doctor identity note under a different label | the note stops being identity | **caught**, both doctor tests fail |

Mutation E was **not caught on the first attempt** — the suite had no test asserting well-scoped
staleness. That gap is the reason `test_well_scoped_staleness_counts_only_that_wells_versions`
exists, and the assertion message names the leak.

### Scope matrix

`plan_rebuild` versions for the three-well fixture (two documents per scope): workspace scope 6,
well A 2, well B 2, no-well 2 — additive and non-overlapping. `scope(delete_derived)` and
`scope(sync_all)` are compared on **row ids**, not counts, in
`test_delete_and_rederive_cover_exactly_the_same_documents`.

### One V4.4 test had to change

`test_a_scope_that_matches_nothing_says_so` asserted, as a precondition, that documents are filed
with `workspace_id IS NULL`. It was testing the defect. It now asserts the opposite as a guard and
triggers the warning through a genuinely non-matching workspace id.

## 8. What is still open

- **Search-index scope** is documented but not re-derived. Index chunks carry a document id, so they
  inherit identity transitively; there is no separate test that retrieval is scoped per well, because
  retrieval is deliberately workspace-wide in this product. That is a documentation fact, not a
  proof of isolation.
- **`W1 → W2` reassignment** is not supported and is not rejected by a dedicated error either: there
  is no move operation to reject. Re-ingesting a corpus under a different workspace creates new
  document rows in the new workspace and leaves the old ones in place, because
  `UniqueConstraint("workspace_id", "identity_path")` is per-workspace. Adding a move feature is out
  of scope for this mission.
- **`versions_without_knowledge`** is 6 on the V4 forensic corpus: six scanned PDFs have artefacts
  but no derivable facts. That is a corpus property, not an identity defect, and it is the honest
  reason `needs_rebuild` is false while the number is non-zero.
