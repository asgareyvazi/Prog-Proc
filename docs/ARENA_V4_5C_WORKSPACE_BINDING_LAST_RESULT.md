# Arena V4.5 continuation — workspace binding, explicit scope validation, structured isolation

Base: `e2cd185` (V4.6, workspace boundary). Everything below was measured against this
repository; no number is carried over from an earlier report.

## 1. What was actually wrong

Two defects survived V4.5 and V4.6, and both let a document acquire a workspace identity that
contradicted the physical workspace processing it.

**An explicit `workspace_id` was trusted.** The rule was "does this row exist in this
database?" — and if it did, the corpus was filed under it. With one workspace row per file
that is harmless by accident. With two rows in the same file it is a direct route from A's
files to B's identity.

**The relocation rule was too permissive.** V4.6 added: exactly one workspace row, its
`root_path` does not match the root this pipeline is bound to, therefore the workspace moved,
therefore update the row. A single path mismatch was treated as proof of a move. Point a
pipeline attached to database A at an unrelated folder B and it repointed workspace A's
`root_path` at B — one ingest call relocating a workspace nobody asked it to touch, with no
error and no second row to show for it.

Reproduced before any change, on real databases (`/tmp/cases_v45c.py`):

| case | root | database | explicit id | before | after |
|---|---|---|---|---|---|
| A | A | A | A | accepted | accepted |
| B | A | A | *(none)* | accepted, identity resolved | accepted |
| C | A | A | B | rejected | rejected |
| C′ | A | A | sibling row in A's own file | **accepted — filed under the sibling** | rejected |
| D | A | A | unknown UUID | rejected | rejected |
| E | B | A | A | **accepted** | rejected |
| F | B | A | *(none)* | **accepted, A's `root_path` rewritten to B** | rejected, A's row untouched |

## 2. Old contract → why it is unsafe → new contract

**Old.** "An explicit `workspace_id` argument still wins; it is an override, not a
suggestion." A green test, `test_an_explicit_workspace_id_still_wins`, asserted exactly that.

**Why it is unsafe.** It is not a feature and it is not legacy compatibility — it is a bug
locked in by a test. The whole point of workspace identity is that the folder a corpus came
from and the row it is filed under agree. Letting a caller assert otherwise means the
platform will happily record "these files belong to workspace B" while physically reading
workspace A, and every downstream scope — search, delete, rebuild, doctor — will faithfully
report the fiction.

**New.** An explicit id is an **assertion the caller may make, not an override it may
exercise**. `IngestionPipeline._assert_workspace_belongs_here` compares it against
`workspace_identity()` — the identity resolved from the root and database this pipeline is
actually bound to. Equal means proceed; unequal means `ValidationError`, before a single
document is written.

**Evidence.** Case C′ above is the discriminating one: a sibling workspace row in the *same*
database satisfies the old existence check and is refused by the new comparison. Cases E and
F are refused for the other reason — the root does not contain this pipeline's database.

`test_an_explicit_workspace_id_still_wins` was **replaced**, not weakened, by
`test_an_explicit_workspace_id_must_match_the_folder_being_ingested`, which asserts the
mismatch is refused *and* that the matching id is still accepted (so this is a validation,
not a ban). The corresponding V4.6 expectation regex was updated to the new message.

## 3. Relocation: which model holds

The brief asked whether "same database, same contents, new root" is a hard invariant, a
convention or just the normal path, and said not to ship a half-working move feature.

**A genuine move carries the database with it.** ADR-0003 makes the workspace's SQLite file
the system of record, and `.drillintel/` lives inside the workspace folder, so moving the
workspace moves its database. That gives a positive signal rather than an inference:

- the new root **contains** this pipeline's database file → a real move → reuse the one row
  and refresh `root_path`;
- it does not → not this workspace's folder → refuse. Neither alternative is acceptable:
  repointing the registered workspace at an unrelated folder is the hijack in case F, and
  registering a second workspace row inside a database that holds one would break the
  one-database-one-workspace rule ADR-0003 states.

`resolve_workspace_id` gained a `database_path` argument and a `_path_contains` helper for
this; when the caller cannot supply a path the containment check is skipped rather than
guessed. `IngestionPipeline.database_path()` derives it from `Database.url` for SQLite and
returns `""` for anything else, so a non-SQLite deployment is not silently held to a rule
that does not apply to it. The CLI passes `workspace.database_path`.

`test_a_real_move_keeps_logical_identity` moves the directory on disk with `shutil.move`,
including `.drillintel`, and asserts the identity is unchanged, no second row appears,
document identity is undisturbed, and ingesting into the moved folder stays in the same
workspace. It also records that merely *opening* the moved folder changes nothing — identity
is resolved when something writes, not on open. `test_a_copy_is_a_separate_workspace` covers
the copy case, where the database does travel, so the row is reused and its path refreshed on
the first ingest.

## 4. Structured isolation, and the answer to "does `search_structured` need a workspace id"

Measured, not assumed (`/tmp/struct_probe.py`): two workspaces, each promoted and indexed,
each with its own index file.

- Alpha's `search_structured` holds well ids `{A-3, B-11}` — both in Alpha's registry — plus
  2 rows with the empty sentinel. Beta's holds its own two, plus 2.
- The well id sets of the two workspaces are disjoint.
- No index contains a well id from the other workspace's registry.

**One `SearchIndex` cannot contain records from two workspace populations.** The index is
built from the registry it sits beside, and each workspace has its own. So `workspace_id` was
**not** added to `search_structured` — the brief said to add it only if two populations in one
index were proven possible, and they are not. `SearchFilters.applies_to_structured` already
carries a comment saying the omission is deliberate; that decision now has a measurement
behind it.

Rows with no well carry the empty sentinel rather than a guessed scope, so the isolation test
accounts for them separately: "unowned" is a real state, not a leak. Their scope is the
database they were promoted into, which the one-index-per-workspace boundary already provides.

**A mutation found the gap.** Removing the `well_id` filter from the structured half of
`SearchFilters` initially broke nothing — the workspace boundary does not depend on it, but
scoping a search to one well *within* a workspace does. `test_well_scoped_structured_search_actually_narrows`
now proves the filter excludes rows (B-11 returns a strict subset of the unscoped set, and the
two wells share no chunk). Writing it also surfaced that the default search limit is small
enough to hide a narrowing effect entirely, so the test sets an explicit limit.

## 5. Other cases from the brief

- **Project/well mismatch (10).** `test_contradictory_project_and_well_are_rejected_not_reconciled`:
  a well owned by project A supplied with project B's id fails with "does not belong to",
  writes nothing, and the well's owning project is unchanged. Supplying the real owner succeeds.
- **No inference from filenames (10).** `test_a_well_is_never_inferred_from_the_filename`: the
  corpus contains files named for A-3 and B-11; every document is filed under the single well
  passed in, none under a well read out of a filename.
- **Duplicate well names (11).** `test_duplicate_well_names_resolve_deterministically`: two
  projects may both hold `A-3`. `find_well("A-3")` returns the same row across repeated calls,
  and `find_well("A-3", project_id=…)` separates the two. No workspace id is needed to
  disambiguate because `Well` is project-scoped by a unique constraint.

## 6. Mutation proofs

Six mutations, each a single-line revert of the new logic, each run against the three identity
suites, each reverted afterwards and verified byte-identical:

| mutation | result |
|---|---|
| A — remove explicit `workspace_id` validation | caught, 5 failing |
| B — accept a workspace id without binding it | caught, 6 failing |
| C — let an unrelated root register a workspace in this database | caught, 2 failing |
| C2 — remove the relocation rule entirely (V4.6 behaviour) | caught, 5 failing |
| D — remove project/well consistency validation | caught, 2 failing |
| E — drop the `well_id` filter from structured search only | caught, 1 failing |

E is the interesting one: it was **not caught** until the narrowing test existed, which is how
the coverage gap in section 4 was found.

## 7. Performance

Identity resolution stays one query and is memoised, so the added validation costs nothing on
the hot path (`/tmp/perf_v45c.py`, measured on a real workspace):

- `workspace_identity()` — cold 1.042 ms, cached 0.00017 ms over 50 calls
- `database_path()` — `/tmp/…/w/.drillintel/database/drilling_intelligence.db`
- ingest of 6 files — 820.5 ms, `ok=True`, `NEW: 6`
- re-resolution after ingest — 0.002 ms

No N+1: the cold path issues the one registry lookup it already did, and the containment check
is a filesystem comparison on a path already in hand.

## 8. Test inventory

New: `tests/integration/test_workspace_binding_v45c.py`, 16 tests — the A–F binding matrix,
the C′ sibling-row case, real-move and copy behaviour, the project/well matrix, no-inference
from filenames, duplicate well names, structured isolation across two workspaces, database
locality of unowned rows, and the well-narrowing proof.

Rewritten: `test_an_explicit_workspace_id_must_match_the_folder_being_ingested` (was
`test_an_explicit_workspace_id_still_wins`); the V4.6 foreign-workspace-id expectation updated
to the new message.

## 9. Scope validation, in one paragraph

One mechanism. `IngestionPipeline.workspace_identity()` resolves the root and database this
pipeline is bound to into a single workspace id; an explicit `workspace_id` is compared
against it and refused on disagreement; `resolve_workspace_id` owns finding or creating the
row and now also owns the containment evidence that distinguishes a move from a mistake;
`KnowledgeScope` remains the one knowledge-layer scope object, and its membership is validated
by the service rather than by the value object. No layer keeps a copy of the rule, `None` never
means global, and the empty string in an index row means unknown rather than all.

## 10. Documents updated

`docs/DECISIONS.md` (ADR-0025 rewritten: the explicit id is validated, not trusted; relocation
needs containment evidence), `README.md` (a convention bullet on workspace binding, and a note
at the pipeline example that `workspace_id` is optional and checked),
`docs/ARENA_V4_5_WORKSPACE_IDENTITY_LAST_RESULT.md` (the "still wins" sentence kept as history
and marked superseded), `docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md` (same sentence corrected).

## 11. Scope agreement after the change

Re-measured on a real workspace after the binding change (`/tmp/scope_equality.py`,
`/tmp/dist_probe.py`), comparing row identities rather than counts:

| scope | value |
|---|---|
| registry, current `DocumentVersion` rows | 6 |
| `status().versions_with_artefacts` | 6 |
| `plan_rebuild()["plan"]["versions"]` | 6 |
| `status(well_id=A-3).versions_with_artefacts` | 6 |
| `status().facts` / `status(well_id=A-3).facts` | 61 / 61 |
| `status().detached_facts`, `needs_rebuild` | 0, False |
| `doctor --json` integrity problems | 0 |

All five agree. The well scope equals the workspace scope here because this corpus files all six
documents under one well — that is the fixture's shape, not a scoping failure; the same call on a
two-well corpus returns the subset (see `test_well_scoped_structured_search_actually_narrows`).

Two things worth recording so nobody re-derives them wrongly:

- `versions_with_artefacts` and `versions_without_knowledge` are **not** a partition. The second
  is a subset of the first (6 and 1 here), so adding them does not give a version total.
- `status(well_id=…)` scopes through the *documents* in that well, which is correct, but a
  `KnowledgeItem`'s own `well_id` column is stamped at derivation and is `NULL` for 10 of the 61
  items here. Counting items by their own column therefore gives 51, not 61 — the two numbers
  answer different questions and neither is a bug.

`doctor` exits 1 on a fresh workspace, on two findings that are both honest: one unresolved
knowledge conflict, and a schema stamp whose head cannot be compared because the migration
scripts are unavailable in this checkout. Neither is identity corruption, and neither was
silenced.

## 12. Regression

**1510 passed, 5 skipped, 0 failed** (`--collect-only` confirms 1515 collected), exit code 0,
run against exactly the tree this document describes.

Seven `src/` files that showed up modified in this worktree were pure `ruff format` reflows;
each was verified to have an AST byte-for-byte identical to `HEAD` before being reverted, so this
change set contains only the binding work.

Gates: `ruff check` and `ruff format --check` clean on all six changed source and test files,
`compileall` clean, `git diff --check` clean, `alembic heads` = `0011`, no migration added or
altered.
