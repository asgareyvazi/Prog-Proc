# Capability inventory — 2026-11-24 (branch `arena/5957b76d-prog-proc`)

Status vocabulary: **DONE** / **PARTIALLY DONE** / **BLOCKED** / **NOT RUN** /
**NOT QUALIFIED** / **OWNER-GATED** / **PLANNED** / **DELIBERATELY EXCLUDED**.
Every claim points at code + tests; text alone is not acceptance. SQLite is the only
qualified backend. Submission ≠ approval; actor strings ≠ authentication.

| # | Area | Status | Evidence |
|---|------|--------|----------|
| 1 | Workspace lifecycle (create/open/doctor) | DONE | `wells/workspace.py`; `test_workspace_lifecycle.py`, `test_doctor_*` |
| 2 | Schema migrations (Alembic, head 0029) | DONE | `migrations/versions/*`; `test_migration_0029.py` (round-trip contract) |
| 3 | 0029 downgrade constraint-preserving rebuild (D-06) | DONE | `test_migration_0029.py` — DDL/FK/index/trigger comparison, FK+integrity check |
| 4 | Well/project/field hierarchy | DONE | `wells/repository.py`; `test_well_section_contract.py` |
| 5 | Well sections plan-vs-actual | DONE | `test_section_population.py`, `test_plan_actual_forensics.py` |
| 6 | DDR manual authoring (Qt + service) | DONE | `operations/ddr.py`; `test_ddr_authoring.py`, `test_ddr_cli.py`, UI workbench |
| 7 | DDR batch PDF import hardened (D-15 containment, D-16 workspace-scoped duplicates, D-17 exact-version queue, D-18 limits) + 2026-11-24: fail-closed workspace identity (D1), TOCTOU-safe single-fd verified staging (D2), once-per-entry progress/cancel contract with cancelled counts (D7) | DONE | `operations/ddr_batch.py`; `test_ddr_batch_import.py` (10 tests) |
| 8 | DDR revision comparison | DONE | `operations/ddr_comparison.py`; `test_comparison_v76.py` |
| 9 | Document intake queue + registration | DONE | `review/intake.py`; `test_intake_registration.py`, `test_intake_e2e.py` |
| 10 | Document registry, immutable versions | DONE | `documents/registry.py`, `documents/repository.py` |
| 11 | Deterministic classification taxonomy | DONE | `classification/taxonomy.py`, `classification/rules.py` |
| 12 | PDF text extraction (real, hash-verified) | DONE | `extraction/pdf_text.py`; `test_source_extraction_integrity.py` |
| 13 | XLSX/DOCX/CSV/text extraction | DONE | `extraction/excel.py`, `docx.py`, `text.py` |
| 14 | Multi-layout PDF extraction golden corpus (D-22) | DONE | `tests/integration/test_multi_layout_corpus.py` — prose/key-value/grid synthetic DDR layouts all classify DDR |
| 15 | OCR / MinerU binary extraction | NOT QUALIFIED | `integrations/mineru/*`; probe reports absent-with-reason; no real inference run |
| 16 | Extraction cache + corruption refusal | DONE | `test_extraction_cache.py`, `test_corrupt_persistence.py` |
| 17 | Source-to-draft with dispositions | DONE | `engineering/source_to_draft.py`; shared `extraction_disposition` events |
| 18 | Program authoring service (sections/targets) | DONE | `engineering/authoring.py`; `test_program_procedure_authoring.py` |
| 19 | Procedure authoring service (steps) | DONE | same; steps incl. hold points, parameters JSON |
| 20 | Program/Procedure CLI | DONE | `engineering/authoring_cli.py`; `test_program_procedure_cli.py` |
| 21 | Program Builder Qt page | DONE | `ui/authoring_pages.py`; `tests/ui/test_authoring_pages.py` (offscreen) |
| 22 | Procedure Builder Qt page | DONE | same; completion state visible, governed transitions (D-09) |
| 23 | Completion-state vocabulary + DB triggers | DONE | migration 0029 + models.py triggers; `test_migration_0029.py` |
| 24 | Governed state transitions (no self-approval) | DONE | `authoring.py` refuses TECHNICALLY_REVIEWED/APPROVED self-assignment (D-09) |
| 25 | Exact Program↔Procedure revision link (NONE/LINKED/STALE) | DONE | migration 0028; `test_program_procedure_authoring.py` |
| 26 | Export JSON/HTML/DOCX/PDF, watermark, no-clobber | DONE | `engineering/export.py`; `test_authoring_export_templates.py` |
| 27 | Export includes completion_state in all formats (D-08) | DONE | same file, `test_procedure_export_includes_completion_state_in_all_formats` |
| 28 | Redaction (path-redaction-v1), canonical records untouched | DONE | `core/redact.py`; export redaction test |
| 29 | Templates (structural only) | DONE | `engineering/templates.py` |
| 30 | Review/approval lifecycle + audit | DONE | `review/`, `core/lifecycle.py`; `test_review_v72.py` |
| 31 | NPT records + `npt.hours_rollup` V1 (only approved calc) | DONE | `engineering/service.py`; `test_npt_rollup_production_path.py` |
| 32 | Calculation registry gate — unapproved methods refused everywhere | DONE | `execute_calculation` + `APPROVED_CALCULATION_METHODS` (Phase Q); `test_e2e_matrix_v1_v15.py::test_v14` |
| 33 | Arbitrary formula engine | DELIBERATELY EXCLUDED | record storage only; no invented formulas |
| 34 | Survey CSV import + Qt page (D-21) + 2026-11-24 contract hardening: duplicate/unknown headers, ragged rows, empty/oversize files, station cap, inconsistent units refused, unknown units preserved verbatim; no trajectory calcs (OWNER-GATED) | DONE | `operations/survey_import.py`, `ui/survey_page.py`; `test_survey_import.py` (10 tests), UI tests |
| 35 | Search (FTS + structured, provenance-grounded) | DONE | `search/`; `test_search_*.py` |
| 36 | Knowledge facts/conflicts/provenance | DONE | `knowledge/`; `test_knowledge_*.py` |
| 37 | Evidence chain + citation forensics | DONE | `evidence/`; `test_evidence_*.py` |
| 38 | Professional reports (HTML/PDF/DOCX) | DONE | `reporting/`; `test_report_*.py` |
| 39 | Audit reporting | DONE | `reporting/audit.py`; `test_report_audit_*.py` |
| 40 | Lessons/best practices/recommendations | DONE | `lessons/`, `operations/promote.py` |
| 41 | Costs (source-value records) | DONE | `engineering/costs.py`; FX/AFE OWNER-GATED |
| 42 | BHA/bit/casing/cement/mud/well-control/HSE records | DONE | `operations/`; promotion test suites |
| 43 | Time breakdown certification | DONE | `test_time_breakdown_certification.py` |
| 44 | Qt workbench navigation incl. Batch DDR Import page (D-14) and Survey Import page | DONE | `ui/main_window.py`, `ui/batch_import_page.py`, `ui/survey_page.py`; `tests/ui/test_authoring_pages.py` (18 tests, offscreen) |
| 45 | Physical Windows display acceptance | NOT QUALIFIED | offscreen only; no physical display evidence |
| 46 | DB backup/restore snapshot (no-clobber, identity-bound) | DONE | `database/backup.py`; `test_workspace_backup.py` |
| 47 | DB concurrency safety | DONE | `test_sqlite_concurrency.py` |
| 48 | PostgreSQL backend | NOT QUALIFIED | SQLite-only qualified; explicit refusal in migrations |
| 49 | Auth/multi-user/signatures | NOT QUALIFIED | actor strings are declared attributions only |
| 50 | Production DR / encrypted archives | NOT QUALIFIED | archive unencrypted by design; DR owner-gated |
| 51 | E2E synthetic matrix V1–V15 with per-scenario scope labels + UI E2E (D-19) | DONE | `test_e2e_matrix_v1_v15.py`; `test_authoring_pages.py::test_ui_e2e_navigation_covers_new_pages` |
| 52 | Full quality suite (unit+integration+UI) | DONE (local) / CI-bound | local full suite 3031 passed; exact-SHA CI runs recorded in the 2026-11-24 mission report |
| 52b | Shard collection reproducibility (Phase B) | DONE | collection job now installs the same pinned constraints as shard jobs; `config.inicfg` replaced by supported ini-file/getini digest; bounded mismatch diagnostics (field diffs, first-10 missing/extra node IDs); 10 adversarial manifest-contract tests |
| 53 | Performance evidence (`out/performance.json`) | OWNER-GATED | protected blob, never read/staged by the agent |
| 54 | Windows run-card / PowerShell acceptance | DONE | `docs/WINDOWS_ACCEPTANCE_RUN_CARD.ps1` (head 0029); CI Windows runs |
| 55 | DDR PDF → record linkage | DONE | `test_ddr_lifecycle.py` — idempotent link, unknown-version refused |
| 55b | Source-to-DDR candidate workflow (D-20 reopened, Phase F) | DONE | `operations/ddr_candidates.py`: labelled field rules with page/line locators, ambiguous→UNRESOLVED (never guessed), accept/edit/reject dispositions preserving original vs edited values, CANDIDATE-only authorize; `test_ddr_candidates.py` (5 tests) |
| 56 | Batch import Qt page (D-14) + 2026-11-24: collision-resistant staging names (E1), identity-based retry against corrected sources (E4), monotonic progress | DONE | `ui/batch_import_page.py`; 3 UI batch tests incl. same-basename and retry-after-fix |
| 57 | Current-facing docs pinned to branch tip (D-12) | DONE | `docs/PRODUCT_CAPABILITY.md`, `PROJECT_CONTEXT.md`, `ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md` @ tip |
| 58 | 2026-11-23 mission report (D-13) | DONE | `docs/mission-reports/PROG_PROC_ENGINEERING_PRODUCT_CONTINUATION_2026-11-23_FA.md` |

| 59 | 2026-11-24 mission report (Phase AB) | DONE | `docs/mission-reports/PROG_PROC_ENGINEERING_PRODUCT_CONTINUATION_2026-11-24_FA.md` |
