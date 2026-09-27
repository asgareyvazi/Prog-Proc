# MISSION_RESULT — V4.5 continuation: workspace binding, explicit scope validation, structured isolation

> **Note on commit references.** This checkout is a shallow clone whose history was
> collapsed to a single grafted commit, so the commits this document describes are not in
> the local object database and their full hashes cannot be verified here. They are cited
> in short form for that reason; `tests/unit/test_report_integrity.py` is what enforces
> that a report never names a full hash the repository cannot produce.

## 1. Verdict

**NOT RELEASE-CERTIFIABLE** — on one gate only: the work cannot be pushed. GitHub credentials in
this sandbox are rejected (`Bad credentials`, `gh auth status` reports the `GH_TOKEN` is no longer
valid, `git push` exits 128, and even `git ls-remote` now fails, so the remote tip could not be
read either). Every engineering gate passes. The commit exists locally and is intact.

## 2. Git state, verified this session

| fact | value |
|---|---|
| base at session start | `e2cd185` (V4.6) |
| commit made | `8a402d5` |
| branch | `arena/01a0c936-prog-proc` |
| worktree | clean |
| commits ahead of last known remote tip | 1 |
| remote tip | **unknown** — `ls-remote` fails on credentials |
| `alembic heads` | `0011`, no migration added or altered |

The brief's stated baseline (remote `6eb4c40`) was stale: `e2cd185` had already been pushed in the
previous session and was confirmed present with `git cat-file -e`.

## 3. Defects found and fixed

**An explicit `workspace_id` was trusted.** It was accepted if the row merely existed in this
database, so a second workspace row in the same file gave a direct route from A's files to B's
identity. `_assert_workspace_belongs_here` now compares the supplied id against
`workspace_identity()` — resolved from the root and database the pipeline is actually bound to —
and refuses a mismatch before writing anything.

**The V4.6 relocation rule was too permissive.** Any single-row path mismatch was read as a move,
so a pipeline attached to database A but pointed at folder B repointed workspace A's `root_path` at
B. Relocation now needs evidence: the new root must contain this pipeline's database file
(`_path_contains` + a new `database_path` argument on `resolve_workspace_id`; the CLI passes
`workspace.database_path`, and `IngestionPipeline.database_path()` derives it from `Database.url`,
returning `""` for non-SQLite so the check is skipped rather than guessed).

## 4. Cases A–F, measured before and after

| case | root | db | explicit id | before | after |
|---|---|---|---|---|---|
| A | A | A | A | accepted | accepted |
| B | A | A | none | accepted | accepted |
| C | A | A | B | rejected | rejected |
| C′ | A | A | sibling row in A's own file | **accepted** | rejected |
| D | A | A | unknown UUID | rejected | rejected |
| E | B | A | A | **accepted** | rejected |
| F | B | A | none | **accepted, A's `root_path` rewritten to B** | rejected, A's row untouched |

## 5. Suspect test

`test_an_explicit_workspace_id_still_wins` was a bug locked in by a test, not a feature or legacy
compatibility. It was **replaced**, not weakened, by
`test_an_explicit_workspace_id_must_match_the_folder_being_ingested`, which asserts the mismatch is
refused *and* that the matching id is still accepted. The V4.6 foreign-id expectation regex was
updated to the new message. No new logic was weakened to keep an old test green.

## 6. Relocation model

A genuine move carries the database with it (ADR-0003 makes the SQLite file the system of record
and `.drillintel/` lives inside the folder). Root contains the database → real move → reuse the one
row and refresh `root_path`. It does not → refuse. `test_a_real_move_keeps_logical_identity` moves
the directory on disk with `shutil.move`, `.drillintel` included, and proves identity, document
identity and row count are unchanged; `test_a_copy_is_a_separate_workspace` covers the copy path.

## 7. Structured isolation

Measured across two indexed workspaces: each index's `search_structured` names only wells from its
own registry, plus 2 rows with the empty sentinel meaning *unowned*. The two workspaces' well id
sets are disjoint. **One `SearchIndex` cannot hold two workspace populations**, so `workspace_id`
was **not** added to `search_structured` — the brief permitted it only if that were proven
possible. Rows with no well are database-local by construction; nothing invents a scope for them.

## 8. Mutations

Six single-line reversions of the new logic, each caught, each reverted and verified byte-identical:
A remove explicit id validation (5 failing), B accept an unbound id (6), C unrelated root may
register in this database (2), C2 remove the relocation rule (5), D remove project/well validation
(2), E drop the `well_id` filter from structured search (1). E was **not** caught until the
narrowing test was written — that is how the coverage gap in §7 was found.

## 9. Scope agreement and performance

status / plan_rebuild / registry / well-scoped status / doctor all agree on 6 versions and 61
facts, with 0 integrity problems. Two traps recorded so they are not re-derived wrongly:
`versions_without_knowledge` is a *subset* of `versions_with_artefacts`, not a partition; and
`status(well_id=…)` scopes through documents, while a `KnowledgeItem`'s own `well_id` column is
`NULL` for 10 of 61 items here — 51 and 61 answer different questions. Identity resolution stays
one memoised query: 1.042 ms cold, 0.00017 ms cached, ingest of 6 files 820.5 ms, re-resolution
0.002 ms. No N+1.

## 10. Regression and gates

**1510 passed, 5 skipped, 0 failed**, exit 0, 1515 collected. 16 new tests in
`tests/integration/test_workspace_binding_v45c.py`. `ruff check` and `ruff format --check` clean on
all six changed source and test files; `compileall` clean; `git diff --check` clean; doc-integrity
tests pass. Seven `src/` files that appeared modified were `ruff format` reflows, each proven to
have an AST identical to `HEAD`, and were reverted so the change set is only this work.

## 11. What is outstanding

Exactly one thing: **the push**. Commit `8a402d5` is local, the worktree is clean, and the change
set is 11 files, +870/−24. When GitHub credentials are restored, `git push origin
arena/01a0c936-prog-proc` completes the certification; nothing else needs to change. Detailed
evidence is in `docs/ARENA_V4_5C_WORKSPACE_BINDING_LAST_RESULT.md`.

**NOT RELEASE-CERTIFIABLE** — Gate O (pushed, SHAs match) blocked on expired GitHub credentials.
