# Arena V4.6 — forensic verification, workspace binding hardening, relocation safety

Everything below was measured against this working tree in this session. No figure is carried over
from a previous report, and the previous report's Git claims are specifically contradicted in §1.

## 1. Repository identity

```text
repo      asgareyvazi/Prog-Proc
branch    arena/01a0c936-prog-proc
baseline  e8621136ca73108ae7b590e6baa72fedc1f00835  (grafted, the only commit in the clone)
remote    origin -> https://github.com/asgareyvazi/Prog-Proc.git
remote    UNVERIFIED - credentials rejected (see §11)
worktree  clean at the end of this mission
```

The clone is **shallow with exactly one reachable commit**. The commits the previous report claimed
— `c279d1c`, `9ecb64d`, `c333dca`, `cbe7760`, `6eb4c40`, `e2cd185`, `8a402d5`, `55ba8a0` — are
**all absent** from the object database, each checked with `git cat-file -e <sha>^{commit}`.

**The previous report's central Git claims were false as of this session.** It stated the work was
"committed as `8a402d5`" with a "worktree clean". Neither held: that commit does not exist, and the
tree carried 42 modified plus 34 untracked paths. What *did* survive was the working-tree content —
the source changes and the test files were all present and passing. The first action of this mission
was therefore to commit that state so it could no longer be lost (`b3f1c6e`), with a message that
says plainly which commits are missing and how that was checked.

## 2. What was actually found

**Confirmed defects, fixed this mission**

1. **A stale comment at the security boundary.** `ingestion/pipeline.py` sat directly above the
   validation call and read *"An explicit `workspace_id` is an override and wins."* The code
   beneath it did the opposite. A comment asserting the unsafe contract, on the line where the safe
   one is enforced, is how the next reader reintroduces the bug. Rewritten to state the real rule.
2. **Two tests whose names contradicted their bodies.**
   `test_a_copied_folder_is_a_different_workspace_because_it_has_a_different_database` never copied
   anything — it created two independent workspaces and asserted they differed, with an `or True`
   making one assertion unconditionally true. Replaced with a test that actually copies, without
   `.drillintel`. Meanwhile `test_a_copy_is_a_separate_workspace` asserted *reuse* while its name
   said *separate*; renamed to say what it proves.
3. **Ten documents cited commit hashes the repository cannot produce**, which
   `test_report_integrity.py` correctly failed on. Shortened to short form with a prose note, per
   the test's own prescribed remedy — the test itself was not weakened.

**Confirmed correct, left alone** (audited, attacked, no defect found)

- Explicit-id binding: compared against `workspace_identity()`, refused before any write.
- Relocation evidence: `_path_contains` on the pipeline's own database path.
- Ambiguous multi-row registry: refused, not guessed.
- `KnowledgeScope` is the single knowledge-layer scope definition; the two raw `Document.*` filters
  outside it are justified — one is read-only diagnostic inside a rolled-back transaction, the other
  is database-local, which ADR-0003 makes workspace-local by construction.
- `resolve_workspace_id` has exactly two production callers (CLI, pipeline); `get_or_create_workspace`
  is reached only through it. No duplicated resolution logic.
- Structured search needs no `workspace_id`: one index cannot hold two workspace populations.

**Remaining limitation:** the copy semantics in §5 are a consequence, not a guarantee — a copy
carrying `.drillintel` inherits the original's identity and cannot be distinguished from a move.

## 3. Changes made

| path | reason | behavioural effect |
|---|---|---|
| `src/drilling_intelligence/ingestion/pipeline.py` | comment asserted the pre-fix contract | none on execution; the rule at that line is now described correctly, and the scan-root/storage distinction is recorded |
| `tests/integration/test_workspace_boundary_v46.py` | copy test proved nothing and contained `or True` | now copies without `.drillintel` and asserts an independent workspace |
| `tests/integration/test_workspace_binding_v45c.py` | name/docstring contradicted the assertions | renamed; documents that a copy inheriting identity is deliberate |
| `tests/integration/test_workspace_relocation_safety_v46.py` | attacks E/F, stale connection and atomicity were untested | 11 new tests |
| `docs/DECISIONS.md` | relocation consequences were undocumented | ADR-0025 records copy, reopen, canonicalization, scan-root and atomicity semantics |
| 10 files in `docs/` | cited unverifiable full SHAs | short form plus a note; `test_report_integrity` passes |
| 24 files (`ruff format`) | formatting drift from the V4 work | none — all 21 `.py` files verified AST-identical |

## 4. Workspace binding result

Measured on real databases, asserting database state and not only the exception:

| root | database | explicit id | result |
|---|---|---|---|
| A | A | none | accepted, identity resolved, documents carry it |
| A | A | A | accepted |
| A | A | sibling row in A's own file | **rejected**, state unchanged |
| A | A | id from another database | **rejected**, state unchanged |
| A | A | unknown UUID | **rejected**, state unchanged |
| A | A | `""` | treated as absent, resolves normally |
| B | A | none | **rejected** — root does not contain the database |
| B | A | A | **rejected** — same reason; a correct id does not launder a wrong folder |
| unrelated | A | none / A | **rejected**, `root_path` and documents unchanged |
| missing | A | A | **rejected**, state unchanged |
| A | A | scan root elsewhere | accepted; documents belong to A, identity unmoved |

## 5. Relocation result

| attack | observed |
|---|---|
| A — unrelated folder | rejected; registry, documents and runs byte-identical |
| B — second real workspace folder | rejected; no second row |
| C — copy **with** `.drillintel` | **accepted as a relocation**: same workspace id, `root_path` refreshed. Documented as deliberate |
| C′ — copy **without** `.drillintel` | refused by the same rule; an empty folder registers itself independently |
| D — move + reopen at new path | accepted; same id, one row, `root_path` refreshed, document ownership intact |
| D′ — move, keep the stale connection | rejected, and **the old database file is not recreated** — the refusal is what stops SQLite writing a fresh empty file at the pre-move path |
| E — folder renamed, database left behind | rejected; state unchanged |
| F — symlinked root | accepted as the same folder; no second row; stored path stays canonical |
| F′ — trailing slash / `.` / `corpus/..` | accepted as the same folder; no duplicate row |

## 6. Scope result

Workspace scope and well scope remain distinct. All six documents in the fixture corpus file under
one well, so a well-scoped status legitimately equals the workspace-scoped one there; the narrowing
is proved separately by `test_well_scoped_structured_search_actually_narrows`, which asserts disjoint
result populations by chunk id rather than by count. Unowned structured rows carry the empty
sentinel and are database-local; nothing invents a scope for them. Conflicts and relations are
database-local, which ADR-0003 makes workspace-local.

## 7. Mutation result

**6 attempted, 6 caught, 0 survived.** Each mutation reverted and verified byte-identical afterwards.

| mutation | failing tests |
|---|---|
| M1 explicit-id equality check neutered | 5 |
| M2 explicit-id validation skipped entirely | 7 |
| M3 relocation evidence predicate removed | 7 |
| M4 no-evidence relocation no longer refused | 11 |
| M5 ambiguous multi-row registry guessed | 1 |
| M6 structured `well_id` filter removed | 1 |

## 8. Test result

See the mission report for the exact collected/passed/skipped/failed/exit figures of the final run.
Targeted workspace suites, run separately: `test_workspace_identity_v45.py`,
`test_workspace_boundary_v46.py`, `test_workspace_binding_v45c.py` and
`test_workspace_relocation_safety_v46.py`.

## 9. Quality gates

```text
ruff check .            All checks passed
ruff format --check .   228 files already formatted (was: 24 would be reformatted)
compileall src tests    exit 0
git diff --check        clean
alembic heads           0011 (head), no migration added or altered
```

## 10. Documentation result

`docs/DECISIONS.md` ADR-0025 now records the four measured relocation consequences — copy inherits
identity, a move requires reopening, paths canonicalize through `expanduser().resolve()` with no
case folding, and `run(root=…)` never moves identity — plus the atomicity guarantee. Ten documents
had unverifiable full hashes shortened with an explanatory note.

## 11. Git result

```text
commits      b3f1c6e (checkpoint landing the working-tree state) + this mission's commits
worktree     clean
remote       NOT VERIFIED
```

`git push` exits **128**; `gh auth status` reports the token is no longer valid and `gh api` returns
`Bad credentials`. `git ls-remote` fails too, so the remote tip could not even be read. No remote SHA
is claimed.

## 12. Final certification

**NOT RELEASE-CERTIFIABLE** — blocked on **Gate R** (remote publication) alone. Every other gate
passes: identity, binding, relocation, atomicity, well isolation, knowledge isolation, structured
isolation, NULL semantics, doctor semantics, race safety, targeted coverage, mutation coverage, the
full suite, quality gates and worktree cleanliness.
