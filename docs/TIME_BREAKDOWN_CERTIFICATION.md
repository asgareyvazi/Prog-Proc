# PROG-PROC — TIME_BREAKDOWN deterministic promotion certification

**Certification date:** 2026-09-22
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0b7fd-prog-proc`
**Source commit inspected:** `b7703baf35abd84881432a93264a979c33751c6c`
**Contract:** `document:TIME_BREAKDOWN:promotion:v2`
**Authority:** checked-out source, the generated source-shaped CSV, stored extraction, migration-backed
SQLite rows, executable tests and Git history.

This is the certification that was deliberately missing from the V3 matrix. It promotes only the
explicit activity/hours table contract already present in the repository; it does not add a generic
workflow, event or calculation framework.

## Admission boundary

A `TIME_BREAKDOWN` version is eligible only when its stored extraction contains a table with both:

- an explicit activity column (`Activity`, `Operation`, or the closed aliases in `ACTIVITY_HEADERS`); and
- an explicit duration column (`Hours`, `Duration`, or the closed aliases in `DURATION_HEADERS`).

The writer does not inspect filenames, folders, narrative prose, search results or an activity label to
infer a domain fact. An explicit `NPT Hours` header is claimed by the NPT table recogniser first and is
not visited again as a generic time breakdown. A duration-only table is not admitted.

## Source-shaped fixture and execution

`tests/fixtures/generate.py::build_time_breakdown_csv` writes a real CSV with five activity rows and a
source total row. The test sends that file through the ordinary scanner, text/CSV extractor, classifier,
stored extraction cache and migration-backed workspace before promotion. The classifier result is
`TIME_BREAKDOWN`; the contract handler is the existing named `report` handler.

The source rows are:

| Source row | Activity | Duration text | Code | Disposition |
| ---: | --- | ---: | --- | --- |
| 2 | Drilling | `8.25` | `DRILL` | actual candidate operation |
| 3 | Tripping | `14.00` | `TRIP` | actual candidate operation |
| 4 | Circulating | `1.50` | `CIRC` | actual candidate operation |
| 5 | NPT - stuck bit | `6.50` | `NPT` | actual candidate operation plus NPT row |
| 6 | NPT - equipment | `12.00` | `NPT` | actual candidate operation plus NPT row |
| 7 | Total | `42.25` | `TOTAL` | excluded aggregation row |

The source duration text for every admitted activity is retained in the operation's source-owned
attributes. Non-NPT activity duration is not silently converted into an engineering calculation. The two
explicit NPT-coded rows retain the existing NPT duration contract and have no fabricated event or root
cause.

## Invariants proven

- **Scope:** every activity row is attached to the registered A-3 well; an unknown named well is refused
  by the existing exact well lookup rather than moved to the document's linked well.
- **State:** rows are `ACTUAL`, `CANDIDATE`, `DERIVED`, and actorless until the existing human confirmation
  lifecycle is used. Planned values are not copied into actual operations.
- **Totals:** the source total is not promoted as a sixth operation or NPT row.
- **Provenance:** the stored table locator is narrowed to each CSV source line (`2` through `6`) without
  inventing a cell locator.
- **Identity:** the existing version/table/row/well identity path is reused; a second promotion creates
  no rows and reports unchanged rows.
- **NPT boundary:** only an explicit NPT code creates an NPT row. Productive, trip and circulation hours
  do not become NPT because they share a duration column.
- **Calculation boundary:** NPT remains the only executable engineering roll-up. Time breakdown promotion
  preserves what the source stated and does not calculate a new engineering value.
- **Read/write boundary:** promotion consumes the stored extraction and search remains disposable; no
  index, review read, or UI selection can authorize a writer.

## Executable gate

```text
/tmp/progproc-venv/bin/python -m pytest -q -rA \
  tests/integration/test_time_breakdown_certification.py \
  tests/integration/test_operations_promotion.py \
  tests/unit/test_promotion_contracts.py
```

**Result:** **26 passed** on the inspected source commit. The focused standalone certification contains
3 tests; the existing operational promotion and static-contract suites contain the remaining 23 tests.
The source was also checked with the repository ruff configuration, `compileall`, `ruff format --check`,
and `git diff --check` before publication.

## Disposition

`TIME_BREAKDOWN` is now **`END_TO_END_CERTIFIED`** at the narrow source-shaped surface above. The
contract remains explicitly bounded: no automatic cost allocation, no inferred productive/NPT
classification, no duration arithmetic for non-NPT operations, no root-cause inference, and no promotion
for a table that does not state both activity and duration.
