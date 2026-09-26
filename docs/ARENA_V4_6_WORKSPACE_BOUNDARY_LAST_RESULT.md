# V4.6 — The Workspace Identity Boundary

Branch `arena/01a0c936-prog-proc`, on top of `6eb4c409451cef041648e93c85c0edd0369f71db`.
Recorded as ADR-0025 in `docs/DECISIONS.md`.

V4.5 fixed documents that were being filed with no workspace at all. This mission covers what that
left open: the ways a **scoped-looking** call can still act on a wider population than its caller
believes. Four defects, each reproduced against the real code before it was changed.

---

## 1. What was still wrong

### `by_identity` quietly became a global search

```python
def by_identity(self, workspace_id: str | None, identity_path: str) -> Document | None:
    stmt = select(Document).where(Document.identity_path == identity_path)
    if workspace_id:                                    # <- falsey means "search everything"
        stmt = stmt.where(Document.workspace_id == workspace_id)
```

Document identity is `(workspace_id, identity_path)` — that is what the schema's
`uq_document_workspace_identity` enforces — so an unscoped lookup is not a narrower question, it is
a different one. Measured before the change, on a corpus ingested into one workspace:

| call | result |
| --- | --- |
| `by_identity(<real id>, path)` | the document |
| `by_identity(None, path)` | **the same document — no error** |
| `by_identity("", path)` | **the same document — no error** |
| `by_identity("ws-does-not-exist", path)` | `None` |

Two production callers (`documents/registry.py:189`, `ingestion/planner.py:203`) both pass a real
id, so this was latent rather than active — but the API invited the mistake, and a caller that
forgot would have received another workspace's document with no signal that anything was wrong.

### Moving a workspace created a second workspace row

`Workspace.root_path` is the identity and it is unique. Move the folder and the path changes, so
resolution found no match and registered a new row — inside the *same* database, which ADR-0003
makes the system of record for exactly one workspace. Measured: one row before the move, **two**
after, and every workspace-scoped query then answering about the wrong half.

### A well and a project that contradict each other were written through

`IngestionPipeline.run` forwarded `well_id` and `project_id` to `registry.register` with no check
that the well belongs to that project. Nothing downstream re-validates it, so the mismatch survives
into every derived row.

### An unscoped document was reachable by forgetting an argument

`create_document(workspace_id=None, …)` was an ordinary call. The column has to stay nullable —
the foreign key is `ondelete="SET NULL"`, so deleting a workspace row orphans its documents on
purpose — but *creating* one is the bug V4.5 spent a mission chasing.

## 2. What changed

| file | change |
| --- | --- |
| `documents/repository.py` | `by_identity` requires a workspace and raises without one; new `any_by_identity(path)` for the deliberate global question, returning a list. `create_document` takes `unscoped: bool = False` and raises on a missing workspace unless it is set. |
| `wells/repository.py` | `resolve_workspace_id` now: exact resolved-path match → reuse **and refresh** `name`/`data_dir` when the caller knows more; no match with exactly one row → that workspace moved, reuse the row and update `root_path`; no match with several rows → `ValidationError`, because attribution would be a guess. |
| `ingestion/pipeline.py` | `_assert_workspace_belongs_here` — an explicit `workspace_id` must be a row *this* database owns. |
| `documents/registry.py` | `register` rejects a `well_id`/`project_id` pair that contradicts the well's actual project. |
| `database/integrity.py` | new `check_workspace_identity`: more than one workspace row, and documents pointing at a workspace row that does not exist. |
| `cli/app.py` | wires that check into `doctor`, ahead of the other integrity problems. |

The refresh-on-match in `resolve_workspace_id` is not cosmetic. Several callers resolve the same
folder and not all of them know everything about it: the pipeline resolves during ingestion and has
no `data_dir`, the CLI resolves with one. Whichever arrives first creates the row, so a caller that
knows more has to be allowed to fill the gap — otherwise the row keeps whatever the first caller
happened to know. That is exactly how `data_dir` came back empty in `test_cli.py` once the pipeline
started resolving before the CLI did.

## 3. What was decided, and what was deliberately not done

**`""` means unknown, never "all".** In the index a document with an empty `workspace_id` satisfies
no workspace filter, and an unscoped query is the caller asking a different question. Verified by
`test_an_unknown_workspace_never_satisfies_a_workspace_filter`. The token was already behaving this
way; what was missing was a test pinning it, because `str(document.workspace_id or "")` at
`search/index.py:474` makes the two look interchangeable at the write site.

**No migration.** The column was already there and already nullable for a reason. `NOT NULL` would
be wrong while the schema's own cascade produces NULLs on purpose.

**No relocation *feature*.** A move is not an operation the user invokes; it is the same folder in a
new place, carrying its own `.drillintel` with it. A copy is therefore a different workspace, because
a copy carries a different database — that is tested rather than assumed.

**A dangling workspace pointer is unreachable through the ORM.** `PRAGMA foreign_keys=ON` rejects
the write, and the test asserts that. `check_workspace_identity` still covers it, because a file
written before that pragma or restored from a partial backup can hold one; the test builds it with
raw SQL and the constraint off for that connection.

**Structured rows remain workspace-unscoped.** `search_structured` has a `well_id` column but no
`workspace_id` one, and `search/index.py:295` says so deliberately. That is now consistent with the
one-workspace-per-file contract, and `check_workspace_identity` is what detects the contract being
broken.

## 4. Verification

### Tests

`tests/integration/test_workspace_boundary_v46.py` — 18 tests, all passing, none mocking the thing
under test. Every one uses a real temporary SQLite database, real Alembic-migrated schema, real
generated corpus files, real ingestion and real derivation.

| brief test | covered by |
| --- | --- |
| A — direct pipeline, no `workspace_id` | `test_direct_pipeline_ingestion_attaches_identity_everywhere` (documents *and* `IngestionRun`) |
| B — a workspace id from another workspace | `test_a_workspace_id_from_another_workspace_is_refused` |
| C — `None` and `""` are not global | `test_none_and_empty_are_not_silently_global` |
| D — same path, two workspaces | `test_the_same_identity_path_in_two_workspaces_stays_distinct` |
| E — no NULL document through the API | `test_create_document_refuses_a_missing_workspace` |
| F — relocation | `test_moving_the_folder_keeps_one_workspace`, `test_reopening_the_same_folder_is_a_no_op`, `test_a_copied_folder_is_a_different_workspace_because_it_has_a_different_database`, `test_ambiguous_attribution_is_refused_not_guessed` |
| G — legacy NULL rows | `test_null_workspace_rows_are_classified_and_never_backfilled` |

Plus: doctor as the identity authority, the index scope token, no hidden global state, repeated
resolution determinism, the project/well relationship, and re-filing a document between wells
compared on `document_id`, `identity_path`, `sha256`, classification, version ids and extraction
rows rather than on counts.

### Mutations

Each is a real change to the shipping code, applied, run against the V4.5 + V4.6 identity suites,
and reverted from a `/tmp` copy:

| mutation | result |
| --- | --- |
| M1 remove the workspace filter from `by_identity` | **caught** |
| M2 `create_document` stops requiring a workspace | **caught** |
| M3 `""` accepted as a global scope | **caught** |
| M4/M5 relocation stops reusing the row | **caught** |
| M6 a well from another project is accepted | **caught** |
| M7 doctor stops checking workspace identity | **caught** |
| M8 an unknown workspace satisfies a workspace filter | **caught** |

### Existing tests that had to change

Five test files and one fixture built documents with `workspace_id=None` on purpose. They now say
so with `unscoped=True`, which is the honest version of what they were already doing. No assertion
was weakened, and no expected count was moved.

`ruff format tests` also re-wrapped thirteen pre-existing files that had never been formatted.
Those changes were reverted: they are unrelated to this mission and would have buried the real diff.

## 5. What is still open

- **Concurrency across processes** is not tested. `Workspace.root_path` is unique, so the database
  breaks the tie rather than the application, and repeated resolution in one process is proven
  deterministic — but two *processes* initialising the same empty workspace at once is not exercised
  here. ADR-0003 puts multi-writer concurrency out of scope by design.
- **Structured retrieval cannot be workspace-isolated by predicate**, only well-isolated. That is a
  schema fact, documented at `search/index.py:295`, and it is safe only while the one-workspace-
  per-file contract holds — which `check_workspace_identity` now enforces visibly.
- **Cross-workspace isolation is proven at the database boundary**, not by putting two workspaces in
  one file. The architecture says that state should not exist, so the tests assert it is detected
  rather than constructing it as a supported configuration.
