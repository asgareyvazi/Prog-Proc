# Product capability inventory

> **Autonomous completion context (2026-11-04):** The product remains local-first, single-user, and **Pre-Alpha**. SQLite is the only qualified database; PostgreSQL is **NOT QUALIFIED**. The 26-class taxonomy and promotion writer set are unchanged. Intake registration is reviewed and non-promoting; DDR authoring/revisions retain actor, reason, provenance, stale-write checks, and review-separated candidate status. The Qt-enabled Linux/Windows CI and identity-bound matrix reports passed on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` (runs [`41804712405`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712405), [`41804712330`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712330)). The same Linux run includes a real Ubuntu 26.04 × CPython 3.14 full-suite lane and passing report. Backup/restore is a hashed, bounded, non-overwriting **database snapshot only**, not a full workspace/source archive. Local Qt is **SKIPPED** here because `libGL.so.1` is absent; physical Windows display acceptance remains **NOT RUN**. M2–M5 remain owner-gated. See [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) and the [Persian final report](mission-reports/AUTONOMOUS_PRODUCT_CLOSURE_2026-11-04_FA.md).

**Status:** source-derived inventory and finite milestone list
**As of:** 2026-11-04 (autonomous completion mission)
**Authority for document classes:** [`DOCUMENT_DOMAIN_COVERAGE.md`](DOCUMENT_DOMAIN_COVERAGE.md)

This file is not a second domain matrix and not a progress percentage. Document
classifications, handlers, and certification levels stay in the coverage matrix.
The classes below are what the source and tests show for the rest of the product.

A README row is not proof. Each class cites the code that implements it and a
test that exercises it, or says that the evidence is only a schema or a comment.

## Classes

| Class | Meaning |
| --- | --- |
| Implemented and end-to-end tested | A caller can use the capability, and a test crosses the public boundary. |
| Implemented with limited coverage | The code path exists; tests cover part of it, or a runtime this checkout did not execute. |
| Architecture or interface only | Types, tables, or settings exist. No behavior uses them. |
| Planned | Named in an ADR or the README, and not implemented. |
| Deliberately excluded | A documented refusal to invent a rule. Not a defect. |
| Unknown | The checkout does not show enough to classify. |

## Inventory

| Capability | Class | Evidence |
| --- | --- | --- |
| Workspace create, marker, and settings load | Implemented and end-to-end tested | `wells/workspace.py`, `config/settings.py`, `tests/unit/test_cli.py` |
| Operator intake, preview, governed disposition, and registry registration | Implemented in services/CLI/UI; Qt UI is **SKIPPED locally** because `libGL.so.1` is absent, while Linux/Windows Qt-enabled exact-SHA jobs passed | `review/intake.py`, `documents/registry.py`, migrations `0024`/`0025`, `tests/integration/test_intake_registration.py`, `tests/integration/test_intake_e2e.py`, `tests/ui/test_intake_workbench.py`, ADR 004. Preview writes no registry/domain row; registration creates document evidence only and does not promote. Runs `41804712405` / `41804712330` on `894161a2f3e296e001c03a7b20b0eeb4f57d5882`; identity-bound full-suite reports also passed. |
| Daily DDR manual authoring workspace | Service lifecycle tests pass; the persisted revision comparison UI test is **SKIPPED locally** because `libGL.so.1` is absent, while Qt-enabled Linux/Windows exact-SHA jobs passed | `operations/ddr.py`, migration `0026`, `review/actions.py`, `ui/main_window.py`, `tests/integration/test_ddr_authoring.py`, `tests/ui/test_workbench.py`. Submit keeps report/operations CANDIDATE. A distinct actor string is not authenticated identity; no auth/multi-user support is claimed. Writes advance `updated_at` monotonically even when the system clock repeats. Runs `41804712405` / `41804712330` on `894161a2f3e296e001c03a7b20b0eeb4f57d5882`. |
| Incremental scan and ingest | Implemented and end-to-end tested | `ingestion/pipeline.py` (`mode="incremental"`), `tests/integration/test_ingestion_pipeline.py` |
| PDF, XLSX, DOCX, text, and CSV extraction | Implemented and end-to-end tested | `extraction/pdf_text.py`, `excel.py`, `docx.py`, `text.py`, generated-corpus tests |
| MinerU for scans, with built-in fallback | Implemented with limited coverage | `extraction/registry.py` wires `MinerUExtractor`; `tests/integration/test_mineru_boundary.py`. A missing runtime stays a fallback. This checkout did not run MinerU. |
| Deterministic classification | Implemented and end-to-end tested | `classification/taxonomy.py`, corpus tests. No model call. |
| Registry, versions, supersession, audit | Implemented and end-to-end tested | `documents/registry.py`, `documents/repository.py`, document invariant tests |
| Knowledge facts, relations, conflicts, human resolution | Implemented and end-to-end tested | `knowledge/service.py`, `tests/unit/test_knowledge_facts.py`, conflict integration tests |
| Domain promotion for the certified classes | Implemented and end-to-end tested | `operations/contracts.py` and the coverage matrix. 14 end-to-end certified, 7 knowledge-supported, 5 extract-only (LESSON_LEARNED admitted in V9.0). |
| DDR, NPT, and operational events | Implemented and end-to-end tested | `operations/promote.py`, forensic corpus tests |
| Engineering records and the NPT roll-up | Implemented and end-to-end tested | `engineering/repository.py`, `engineering/costs.py` persists source costs and does not convert currency. NPT roll-up is the only executable method. |
| Search, retrieval, evidence packages | Implemented and end-to-end tested | `search/service.py`, `retrieval/service.py`, `evidence/service.py`, forensic tests |
| Citation verification | Implemented and end-to-end tested | `evidence/verify.py`, `reporting/audit.py`. Opt-in. A match is not full coverage. |
| Field intelligence, timeline, patterns, recommendations | Implemented and end-to-end tested | `intelligence/field.py`, `timeline.py`, `patterns.py`, `tests/integration/test_field_intelligence.py` |
| Decision and comparison packs | Implemented and end-to-end tested | `intelligence/decision.py`, `comparison.py`, V7.5 and V7.6 tests |
| Analyst question catalog | Implemented and end-to-end tested | `intelligence/analyst.py`. Fixed question ids. No natural-language parser and no model. |
| Engineering report and source lineage | Implemented and end-to-end tested | `reporting/service.py`, `lineage.py`, report tests. Renderers issue no SQL. |
| Desktop Review Workbench | Implemented with UI CI evidence; manual desktop acceptance remains open | `ui/main_window.py` reads via `DomainReviewService` and mutates via `ReviewActionService` with `ReviewActionWorker` outside the GUI thread. The DDR lifecycle/comparison test is **SKIPPED** locally because Qt cannot load `libGL.so.1`; Linux/Windows Qt-enabled jobs and their offscreen coverage/stability gates passed on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` (runs `41804712405` / `41804712330`). The UI-package gate measured 76.43% lines / 54.21% branches versus 70/40 minimums. Physical Windows display acceptance remains **NOT RUN**. |
| SQLite lifecycle and migrations | **SQLite is qualified for the documented local single-user use**; migrations through `0026` pass on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` | Alembic migrations `0025`/`0026`, `database/migrations.py`, migration and integrity tests; Linux/Windows runs `41804712405` / `41804712330`. PostgreSQL remains **NOT QUALIFIED**. |
| SQLite storage health / doctor preflight | Implemented; exact-SHA Linux/Windows suite evidence **PASS** on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` | `database/sqlite_health.py` runs read-only integrity/FK/sidecar checks before bootstrap and rechecks after database creation/upgrade; a failed preflight blocks domain/search health claims. No automatic repair. Tests: `tests/unit/test_sqlite_health.py`, `tests/integration/test_doctor_storage_health.py`; exact CI runs `41804712405` / `41804712330`. |
| SQLite database snapshot backup and restore (not full workspace) | Implemented with bounded resource and publication safeguards; exact-SHA Linux/Windows suite evidence **PASS** on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` | `database/backup.py` uses SQLite online backup, SHA-256 manifest, schema revision, integrity/FK checks and no-overwrite publication. It includes only `workspace.toml` plus DB snapshot (8 GiB DB, 16 MiB archive overhead, 1 MiB metadata, exactly three archive members); source corpus and disposable search index are excluded. Restore is only to a missing destination and enforces the stored workspace-root identity; re-home is refused. Archive is unencrypted. Tests: `tests/integration/test_workspace_backup.py`; exact CI runs `41804712405` / `41804712330`. This is **not full workspace recovery**. |
| Package, CLI, and release smoke | Implemented and end-to-end tested | `pyproject.toml` scripts `drillintel` and `drillintel-ui`, `tools/release_smoke.py`, clean-install CI job |
| Ollama probe, completion, and embedding | Implemented with limited coverage | `integrations/ai/ollama.py`, `tests/unit/test_ai_provider.py`. Success requires a finite vector per input and an exact model name. Not called by extraction, promotion, search, or citation. A live Ollama is opt-in via `DRILLINTEL_AI_LIVE=1` and is not a CI pass. |
| OpenAI-compatible provider | Architecture or interface only | `AiSettings.provider` accepts the name. No adapter sends a request. |
| Vector store and semantic ranking | Architecture or interface only | `UnavailableVectorStore` reports the gap. `[search]` vector keys are parsed and unused (`configs/development.toml`). |
| Skills | Architecture or interface only | `database/models.py` `Skill` and `SkillVersion`. No service reads them. |
| Additional calculation engines | Planned | README and ADR policy. NPT roll-up V1 is the only registered executable method. |
| Risk scoring, cost/AFE engine, plan-vs-actual dashboards | Deliberately excluded | `docs/DOMAIN.md`. Cost rows keep source currency. Comparison refuses unit conversion. |
| Trajectory mathematics | Planned | ADR names a later `wellpathpy` wrapper. Survey stations are stored as reported. No interpolation in the directional-survey contract. |
| Ubuntu 26.04 × CPython 3.14 CI lane | **PASS on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` (matrix lane only)** | Real Qt-enabled full-suite job `126488762978`, run `41804712405`: 2,770 collected, 2,769 passed, 1 skipped, 0 failed/errors; clean-install smoke and identity-bound totals report passed. Artifact `pytest-ubuntu-2604-cp3.14-a1` ID `13310917801`. This does not claim general release qualification or physical Windows acceptance. |
| Windows install and offscreen UI | CPython 3.11/3.14 suite, clean install, Qt-enabled UI/stability and PowerShell run-card tests **PASS** on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` | `tools/venv_executables.py`, `tools/windows_pilot.ps1`, `.github/workflows/ci-windows.yml` on `windows-2022`, `docs/WINDOWS_TESTING.md`, run `41804712330` (14/14 jobs success). Offscreen CI and disposable pilot are not manual desktop acceptance; physical display acceptance remains **NOT RUN**. |

## Closure blockers and unresolved defects — single register

This section is the closure ledger for the capability inventory above. It distinguishes a product defect from absent qualification evidence or an owner decision; historical reports are not closure evidence for a later SHA.

| ID | Item | State | Closure evidence / owner action |
| --- | --- | --- | --- |
| Q-01 | Linux/Windows CPython 3.11/3.14 full-suite attribution and exact-SHA evidence | **PASS on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882`** | Identity-checking reporters passed for Linux run `41804712405` (9/9 jobs) and Windows run `41804712330` (14/14 jobs); both runs name this exact commit. Matrix report artifacts: Linux `13312715966`, Ubuntu lane `13312421750`, Windows `13311839332`. The documentation-only commit containing this inventory is independently revalidated in CI. |
| Q-02 | Real Qt-enabled UI and lifecycle tests | **PASS in offscreen Qt CI on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882`** | Headless local UI tests skip because `libGL.so.1` is absent. Linux and Windows offscreen Qt-enabled jobs executed DDR/intake/workbench tests, stability checks, and a UI coverage gate (76.43% lines / 54.21% branches) successfully in runs `41804712405` / `41804712330`. Physical Windows display acceptance remains tracked separately as Q-04. |
| Q-03 | Ubuntu 26.04 with CPython 3.14 | **PASS for this CI matrix lane only** on code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` | Job `126488762978` in run `41804712405`: 2,770 collected, 2,769 passed, 1 skipped, 0 failed/errors; exact-SHA attribution report passed. This closes the missing Ubuntu 26.04 × 3.14 CI lane but does not qualify the product for general release. |
| Q-04 | Physical Windows desktop display acceptance | **NOT RUN** | Requires a human operator on a real Windows desktop; offscreen CI and the disposable pilot are not substitutes. |
| Q-05 | PostgreSQL backend | **NOT QUALIFIED** | SQLite remains the only qualified backend. A product target and a real PostgreSQL service/migration/integration matrix are prerequisites; no assumption or skipped test closes this item. |
| O-01 | M2–M5: external AI adapter, vector sidecar, Skills runtime, additional engineering method | **OWNER-GATED** | Remain outside this completion scope until the product owner approves contracts, data boundaries, security and acceptance cases. |
| O-02 | Trajectory/directional calculations | **OWNER-GATED / calculations disabled** | Survey station storage may remain; no calculation is enabled without a reviewed method, units, assumptions, reference and refusal contract plus independent verification. |

## Historical milestone note — V9.0

The following section records historical V9.0 scope; it is not a current acceptance claim.
V9.0 delivered one source-grounded product capability (M6) and proved the
entire operator workflow. LESSON_LEARNED moves from `KNOWLEDGE_SUPPORTED` to
`END_TO_END_CERTIFIED` via `lesson_learned` handler (contract `v8`), with
migration `0020` adding promotion linkage to `lesson_learned`. The tally
requires a `lesson` column plus `lesson_code` or `title`; each row is one
`lesson_learned` kept verbatim with provenance, well/field/project scope,
problem type, observation, context, recommendation, root cause (UNKNOWN when
absent), applicable operations and hole size. Narrative TXT lessons stay
knowledge evidence (refused by shape). Identity is `promotion_identity`;
revision supersedes by document; lessons start `DRAFT` and require evidence
plus second reviewer for `APPROVED`. No inference beyond source-stated facts.

It also corrects README's read-only claim to describe the governed
`ReviewActionService` boundary, and proves the workflow from workspace
creation through ingest, promotion, knowledge conflict, search, evidence,
decision/comparison/report and governed review.

Exit criteria for this milestone:

- `LESSON_LEARNED` XLSX tally promotes to `lesson_learned` with provenance and `DRAFT`; TXT without a table is `UNSUPPORTED`.
- Duplicate, cross-well, missing-field, ambiguous and revision cases are refused or superseded as specified (12 corpus tests).
- A promoted lesson can be approved only with evidence and a different reviewer; stale and self-approval are rejected.
- Full workflow test crosses the public boundary from workspace creation to report.
- README and WINDOWS_TESTING describe the `DomainReviewService`/`ReviewActionService` split correctly.
- Alembic head is `0020`.

The blocked milestones below are specified further in
[`PRODUCT_DECISIONS_REQUIRED.md`](PRODUCT_DECISIONS_REQUIRED.md). That file
is the decision register. This file remains the capability inventory.

## Finite milestones after this one

These are the next product gaps. A later mission should take one of them, not
all of them.

| Milestone | What is missing | Why it waits | Acceptance test |
| --- | --- | --- | --- |
| M2. OpenAI-compatible adapter | A real HTTP adapter for the name already accepted in settings | The ADR chose Ollama. The cloud URL, auth header, and whether `ai status` may leave the machine are not specified. | Mocked 401, 404, and 200 tests; no request when the env key is absent; no key in logs; engineering packages still do not import it. Blocked — see `PRODUCT_DECISIONS_REQUIRED.md` M2 packet. |
| M3. Non-authoritative embedding sidecar | A place to store vectors and a similarity rule | ADR-0005 rejected Chroma. Dimension, metric, and whether a sidecar may affect retrieval are unspecified. | A rebuild can drop the sidecar; retrieval answers do not change when it is absent; no domain table gains a vector column without a migration test. Blocked — see M3 packet. |
| M4. Skills runtime | Behavior for the `skill` tables | The tables have no service and no operator contract. | A skill cannot become a calculation or a promoted row unless a written contract says so. Blocked — see M4 packet. |
| M5. One additional calculation method | Any engine besides NPT roll-up | The method, units, and failure mode are not specified. | The method id, inputs, and refusal cases exist before the code, and review does not recalculate. Blocked — see M5 packet. |
| M6. Lesson-learned tally (V9.0) | **Delivered** — source-shaped lesson writer | Was knowledge-supported without a handler; a narrative TXT is evidence only. | **Done:** `LESSON_LEARNED` XLSX tally → `lesson_learned` (v8), 12 forensic tests, provenance, idempotence, governance. |

## Deliberately not scheduled

Risk scores, currency conversion, AFE rules, plan-versus-actual dashboards, and
inferred survey geometry stay excluded until the project owner specifies the
rule. Their absence is not a defect in this inventory.
