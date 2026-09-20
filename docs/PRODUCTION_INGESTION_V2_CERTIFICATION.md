# Production ingestion & domain promotion V2 certification

**Certification date:** 2026-09-20
**Branch:** `arena/01a0b7fd-prog-proc`
**Workspace HEAD inspected before the certification commit:** `b2e76fad64e9b8acac187af45eb9bb8d9d1175c5`
**Scope:** stored-artefact ingestion, static classification coverage, explicit domain promotion, knowledge/evidence preservation, review/CLI exposure, and the sole executable NPT V1 calculation.

This is a forensic certification record, not a claim that every taxonomy class has a domain model. The
coverage matrix is the authority for those limits. Unsupported is a successful safety outcome when no
source-derived domain destination exists.

## Certified architecture

| Boundary | Certification statement | Evidence |
| --- | --- | --- |
| Registry/versioning | File identity is workspace-relative; versions are content-addressed, current/history and immutable; failed processing states remain visible. | `documents/repository.py`, `documents/registry.py`, ingestion/versioning tests |
| Extractor choice | Router records candidates, chosen parser, fallback/degraded state and cache key before extraction; cached artefacts retain parser/provenance. | `extraction/router.py`, `extraction/registry.py`, `test_extraction_cache.py` |
| Classification | Deterministic taxonomy evidence and confidence are retained; a classifier enum is not a domain permission. | `classification/taxonomy.py`, `classification/rules.py` |
| Knowledge | Stored fields are re-derived without re-reading files; quality/provenance/conflict/state are preserved and planned/actual are not mixed. | `knowledge/service.py`, `knowledge/facts.py`, knowledge forensics tests |
| Promotion | Complete static registry; only four handlers; no arbitrary table/folder fallback; required source evidence is checked before a writer. | `operations/contracts.py`, `operations/promote.py`, `test_promotion_contracts.py` |
| Operational identity | Promoted records use version/table/row/well identity keys; same-version re-runs are idempotent; source changes conflict rather than overwrite human edits. | `operations/promote.py`, `test_operations_promotion.py` |
| Scope/linkage | Row-named wells are resolved through the authoritative well repository; unknown wells are refused; field/project reads traverse well scope. | operations and field-scope tests |
| Review/UI | Review is read-only and exposes evidence, unsupported/non-certified document flags, conflicts, source navigation, current/history and calculation staleness. The explicit NPT button is the only UI execution path. | `review/service.py`, `ui/main_window.py`, review/UI tests |
| Search | Search/index is disposable discovery only; authoritative reads verify domain/calculation state. | search/retrieval contracts and tests |

## Golden corpus

The miniature is generated from real PDF, XLSX, DOCX, CSV, TXT and scanned-PDF builders into pytest's
temporary directory. The manifest is [`tests/golden_corpus/manifest.json`](../tests/golden_corpus/manifest.json).
It covers:

- one drilling-program plan (planned target, unit and revision/history semantics);
- one DDR activity table (NPT-code gate and total-not-double-counted rule);
- one shared NPT CSV (row well linkage, unit conversion, zero/unknown refusal and unknown-well refusal);
- one mud report and one lesson file that remain unsupported for domain promotion;
- one degraded scanned PDF that must not fabricate text or domain rows.

## Exact validation record

The commands below are the certification gate. Counts must be copied from the command output when the
final clean environment is run; no count is inferred from the stale README baseline.

| Gate | Command | Result at final run |
| --- | --- | --- |
| Focused V2 contracts | `python -m pytest -q tests/unit/test_promotion_contracts.py` | **5 passed** |
| Golden operational corpus | `python -m pytest -q tests/integration/test_operations_promotion.py tests/integration/test_program_promotion.py` | **58 passed** |
| Knowledge/extraction provenance | `python -m pytest -q tests/integration/test_knowledge_pipeline.py tests/integration/test_knowledge_forensics.py tests/integration/test_extraction_cache.py` | **54 passed** |
| Review/calculation production path | `python -m pytest -q tests/integration/test_domain_review.py tests/integration/test_npt_rollup_production_path.py tests/ui` | **30 passed, 3 skipped** (Qt loader: `libGL.so.1` unavailable) |
| Full tests | `python -m pytest -ra` | **1083 passed, 3 skipped** in 524.29s |
| Compile | `python -m compileall -q src tests migrations` | **passed** |
| Lint | `ruff check src tests migrations --output-format=concise` | **passed: `All checks passed!`** |
| Format | `ruff format --check src tests migrations` | **passed: `178 files already formatted`** |
| Build/install | `python -m build`, clean wheel install, `drillintel --version`, `drillintel records promote --help` | **passed**; wheel/sdist and clean CLI/import checks passed |

The final report must include pass/fail/skip counts, exact lint/format output, artifact names and hashes,
clean installed-artifact checks, and Git/remote verification. A Qt-unavailable host is a documented
optional skip, not a hidden failure; the headless checks remain mandatory.

### Artifact and clean-install evidence

The final build produced these exact artifacts (SHA-256):

| Artifact | SHA-256 |
| --- | --- |
| `drilling_intelligence-0.0.1a0-py3-none-any.whl` | `b5b5deb7dd9a0aa208938c69c3a6960f2990931126fb207d71655e67215f1979` |
| `drilling_intelligence-0.0.1a0.tar.gz` | `b2ea25ccae035bb562a520c5ea29439db1a9894616e174526070cc173e6c0150` |

A fresh `/tmp/proc-v2-clean` virtual environment installed the wheel without the source checkout on
`sys.path`. It imported the package and the complete 26-entry contract registry, printed
`0.0.1a0` with `drillintel --version`, and rendered `drillintel records promote --help`, including
`--include-unsupported`. The default wheel intentionally contains no Qt dependency; the optional Qt
smoke was attempted with PySide6-Essentials 6.11.2, but the host's Qt loader could not load
`libGL.so.1`, so the three QWidget tests remained explicit skips.

## Safety findings

- `DRILLING_PROGRAM`, `DDR` and `NPT` are end-to-end certified; `TIME_BREAKDOWN` is a restricted
  handler but remains `DOMAIN_PROMOTABLE` until a standalone classification fixture is certified.
- `MUD_REPORT`, `COST`, `PROCEDURE`, `LESSON_LEARNED` and all other non-handler classes never become
  operational rows through a generic fallback. `records promote --include-unsupported` makes that
  denial visible for a corpus audit.
- No approval stamp is converted into human domain confirmation. Promoted rows start as candidates.
- No parser, knowledge derivation, review load, index update or stale calculation state executes NPT.
  Execution is explicit, actor-bearing and unit/provenance validated.
- A source edit, missing locator, missing well, ambiguous section or invalid field is a named outcome,
  not a silent zero or nearest-match repair.
