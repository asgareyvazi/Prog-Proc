# Arena V4.6 — forensic verification, workspace binding hardening, relocation safety

Everything below was measured against this working tree in this session. No figure is carried over
from a previous report, and the previous report's Git claims are specifically contradicted in §1.

## 1. Repository identity

```text
repo      asgareyvazi/Prog-Proc
branch    arena/01a0c936-prog-proc
baseline  e8621136ca73108ae7b590e6baa72fedc1f00835  (grafted root of this clone)
work      71112e890adea8caf6b4898026251283611de654  (merge of this work with e2cd185)
remote    origin -> https://github.com/asgareyvazi/Prog-Proc.git
remote    VERIFIED at 71112e890adea8caf6b4898026251283611de654 by two independent paths
worktree  clean
```

`71112e8` is named as the **work** commit, deliberately: a document cannot contain the hash of the
commit that carries it, so the carrying commit is left to `git log`. Everything measured below
describes the tree at `71112e8`.

At session start the clone held **exactly one reachable commit**, and every commit the previous
report named — `c279d1c`, `9ecb64d`, `c333dca`, `cbe7760`, `6eb4c40`, `e2cd185`, `8a402d5`,
`55ba8a0` — was absent from the object database, each checked with `git cat-file -e <sha>^{commit}`.

That absence turned out to be a property of the *clone*, not of the work. `git ls-remote` showed the
remote branch sitting at `e2cd185`, and a `--depth=50` fetch brought the whole chain back:
`e862113` -> `c279d1c` -> `c333dca` -> `cbe7760` -> `6eb4c40` -> `e2cd185`. So the previous report's
claim that `e2cd185` had been pushed was **true**; what was false was its claim that `8a402d5` was
committed and the worktree clean. Those two commits were the V4.5-continuation work, they were never
pushed because authentication failed at the time, and they did not survive the re-clone. The
workspace-binding fix existed in this session only as uncommitted working-tree content.

The first action of this mission was therefore to commit the working-tree state so it could no
longer be lost (`b3f1c6e`), with a message saying plainly which commits were missing and how that was
checked. Reconciliation with the remote is described in §11.

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
4. **Four reports and one test asserted a false ancestry.** They said the V3 baseline `b7703baf` was
   "not an ancestor of HEAD". It is — four commits back, immediately before the V4 work, confirmed by
   `git merge-base --is-ancestor` exiting 0. The claim was never verified; it was inferred from the
   commit being absent in a shallow clone, which cannot distinguish "predates the fetch boundary"
   from "is not on this branch" — the exact ambiguity a sibling test in the same file exists to
   police. The reports were corrected and the test rewritten to assert the true relationship: the
   baseline exists, is behind HEAD rather than equal to it, and predates the V4 boundary commit.

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

Full suite, on the exact tree that was pushed:

```text
collected  1526
passed     1523
skipped    3
failed     0
errors     0
exit code  0
```

Targeted boundary suite, run separately — 106 tests, all passing:
`test_workspace_identity_v45.py` (27), `test_workspace_boundary_v46.py` (18),
`test_workspace_binding_v45c.py` (16), `test_workspace_relocation_safety_v46.py` (9), plus
`test_report_integrity.py`, `test_race_safety.py`, `test_document_invariants.py` and
`test_structured_index_boundary.py`.

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

Authentication, which had failed earlier in the session, worked when retried. The first push was
**rejected** — the remote branch was at `e2cd185` and this clone had been rebuilt from `e862113`, so
the two had diverged. Rather than force-push or rewrite anything, the remote branch was fetched and
**merged**.

The merge was checked before it was trusted. `git diff --name-status` proved no file exists on the
remote that this tree lacks, and an AST comparison of every differing `src/*.py` showed only three
files differ semantically — `cli/app.py`, `ingestion/pipeline.py` and `wells/repository.py`, which is
exactly the V4.5-continuation binding fix the remote never received; every other difference was
formatting. After resolving, `git diff <pre-merge-HEAD> HEAD` was **empty**, proving the merge
discarded nothing from either side.

```text
commits    b3f1c6e  land the working-tree state the re-clone had orphaned
           8b4ee56  harden the relocation boundary; correct the stale comment and two tests
           a79bcbd  shorten unverifiable hashes; clear formatting drift
           56d396d  correct the false V3-baseline ancestry claim
           71112e8  merge e2cd185 (the remote tip) - HEAD
worktree   clean
remote     refs/heads/arena/01a0c936-prog-proc = 71112e890adea8caf6b4898026251283611de654
```

Remote verified twice, by independent paths: `git ls-remote origin` and
`gh api repos/asgareyvazi/Prog-Proc/branches/arena/01a0c936-prog-proc` both returned `71112e8…`
immediately after the push.

The credentials then expired again a few minutes later, so the commit carrying *this document* could
not be pushed. That is stated rather than papered over: the pushed tree `71112e8` contains every
source and test file described here — `git diff --name-only 71112e8 HEAD` lists one file, this
document, and no `.py` path. The release candidate is published; the record of its certification is
the one thing that is not.

## 12. Final certification

**RELEASE-CERTIFIABLE**

Every gate passes, including Gate R: the remote branch verifiably contains this tree.

| gate | result |
|---|---|
| A repository identity | branch and HEAD verified; baseline and merge parents recorded |
| B source inspection | resolution, binding, relocation and scope paths read directly |
| C explicit-id binding | sibling, external and unknown ids all refused before any write |
| D relocation safety | unrelated folder, renamed folder and second workspace all refused |
| E valid relocation | move + reopen preserves identity; copy semantics measured and documented |
| F no partial mutation | registry, documents, ownership and run trail asserted unchanged |
| G well isolation | disjoint result populations asserted by chunk id, not count |
| H knowledge isolation | one scope definition; the two raw filters justified individually |
| I structured isolation | one index cannot hold two populations; no redundant filter added |
| J NULL/orphan semantics | nullable preserved, never backfilled |
| K doctor semantics | empty, stale, orphaned and corrupt kept distinct |
| L race safety | repeated resolution yields one row |
| M targeted coverage | 106 tests pass |
| N mutation coverage | 6 attempted, 6 caught, 0 survived |
| O full suite | 1526 collected, 1523 passed, 3 skipped, 0 failed, exit 0 |
| P quality gates | ruff check clean, format clean, compileall 0, diff-check clean, alembic 0011 |
| Q cleanliness | worktree clean |
| R remote publication | remote = `71112e8` = the certified tree, verified two ways; the later documentation-only commit is unpushed because the token expired |
