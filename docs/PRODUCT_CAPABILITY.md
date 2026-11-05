# Product capability inventory

> **Current qualification context (2026-11-06):** The product remains local-first, single-user, and **Pre-Alpha**; SQLite is the only qualified database, PostgreSQL is **NOT QUALIFIED**, and the 26-class taxonomy is unchanged. The code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` passed Linux [`41940002906`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002906) and Windows [`41940002911`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002911) exact-SHA workflows (23/23 jobs). Five full-suite lanes passed; skips remain separately identified. Windows offscreen UI was 29/29 on CPython 3.11 and 3.14; the earlier registration/refresh failure was not reproduced, but its root cause remains unproven and no physical Windows display acceptance is claimed. Wheel/sdist clean-install and schema bootstrap to `0026` passed. The backup and archive commands retain separate scopes; the archive is unencrypted and external sources remain out of scope. M2–M5 and the newly surfaced source-lesson removal/code-change question remain owner-gated. This paragraph is the current system-wide qualification summary; individual rows below can cite historical exact-SHA tests and must not be read as results for `b3163bf...` unless explicitly stated. See [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) and the [Persian final report](mission-reports/AUTONOMOUS_PRODUCT_CLOSURE_2026-11-04_FA.md).


**Status:** source-derived inventory and finite milestone list
**As of:** 2026-11-06 (autonomous product closure; system-wide exact-SHA status is in the current context block above)
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
| Operator intake, preview, governed disposition, and registry registration | Implemented in services/CLI/UI; Qt UI is **SKIPPED locally** because `libGL.so.1` is absent; current Linux/Windows Qt-enabled exact-SHA jobs passed | `review/intake.py`, `documents/registry.py`, migrations `0024`/`0025`, `tests/integration/test_intake_registration.py`, `tests/integration/test_intake_e2e.py`, `tests/ui/test_intake_workbench.py`, ADR 004. Preview writes no registry/domain row; registration creates document evidence only and does not promote. Runs `41940002906` / `41940002911` on `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`; Windows UI 29/29 on both runtimes. A historical registration/refresh failure was not reproduced and its root cause is not established. |
| Daily DDR manual authoring workspace | Service lifecycle tests pass; the persisted revision comparison UI test is **SKIPPED locally** because `libGL.so.1` is absent, while current Qt-enabled Linux/Windows exact-SHA jobs passed | `operations/ddr.py`, migration `0026`, `review/actions.py`, `ui/main_window.py`, `tests/integration/test_ddr_authoring.py`, `tests/ui/test_workbench.py`. Submit keeps report/operations CANDIDATE. A distinct actor string is not authenticated identity; no auth/multi-user support is claimed. Writes advance `updated_at` monotonically even when the system clock repeats. Runs `41940002906` / `41940002911` on `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`. |
| Incremental scan and ingest | Implemented and end-to-end tested | `ingestion/pipeline.py` (`mode="incremental"`), `tests/integration/test_ingestion_pipeline.py` |
| PDF, XLSX, DOCX, text, and CSV extraction | Implemented and end-to-end tested | `extraction/pdf_text.py`, `excel.py`, `docx.py`, `text.py`, generated-corpus tests |
| MinerU for scans, with built-in fallback | Implemented with limited coverage | `extraction/registry.py` wires `MinerUExtractor`; `tests/integration/test_mineru_boundary.py`. A missing runtime stays a fallback. This checkout did not run MinerU. |
| Deterministic classification | Implemented and end-to-end tested | `classification/taxonomy.py`, corpus tests. No model call. |
| Registry, versions, supersession, audit | Implemented and end-to-end tested | `documents/registry.py`, `documents/repository.py`, document invariant tests |
| Lesson code identity, lifecycle, and source revisions | Implemented and regression-tested; source removal/rename semantics remain owner-gated | `lessons/repository.py`, `operations/promote.py`, migrations `0020`–`0023`; canonical strip + Unicode casefold global uniqueness is enforced in repository/promotion and by the current-row partial unique index. Supersession preserves predecessor `prior_status`; a new successor starts DRAFT without inherited approval. Focused lesson/exception tests 95/95 and exact-SHA CI PASS on `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`. Deletion or code change in a source without explicit successor requires owner policy; do not infer automatic disposition. |
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
| Desktop Review Workbench | Implemented with current Qt CI evidence; manual desktop acceptance remains open | `ui/main_window.py` reads via `DomainReviewService` and mutates via `ReviewActionService` with `ReviewActionWorker` outside the GUI thread. The DDR lifecycle/comparison test is **SKIPPED** locally because Qt cannot load `libGL.so.1`; Linux/Windows Qt-enabled jobs and offscreen coverage/stability gates passed on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` (runs `41940002906` / `41940002911`). Windows UI passed 29/29 in CPython 3.11 and 3.14. Physical Windows display acceptance remains **NOT RUN**; root cause of the earlier Intake refresh timeout remains unknown. |
| SQLite lifecycle and migrations | **SQLite is qualified for the documented local single-user use**; migrations through `0026` pass on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` | Alembic migrations `0025`/`0026`, `database/migrations.py`, migration and integrity tests; exact Linux/Windows runs `41940002906` / `41940002911`, plus wheel/sdist metadata-bootstrap smoke. PostgreSQL remains **NOT QUALIFIED**. |
| SQLite storage health / doctor preflight | Implemented; exact-SHA Linux/Windows suite evidence **PASS** on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` | `database/sqlite_health.py` runs read-only integrity/FK/sidecar checks before bootstrap and rechecks after database creation/upgrade; a failed preflight blocks domain/search health claims. No automatic repair. Tests: `tests/unit/test_sqlite_health.py`, `tests/integration/test_doctor_storage_health.py`; exact CI runs `41940002906` / `41940002911`. |
| SQLite database snapshot backup and restore | Implemented with bounded resource and publication safeguards; exact-SHA Linux/Windows suite evidence **PASS** on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` | `database/backup.py` uses SQLite online backup, SHA-256 manifest, schema revision, integrity/FK checks and no-overwrite publication. It includes only `workspace.toml` plus DB snapshot (8 GiB DB, 16 MiB archive overhead, 1 MiB metadata, exactly three archive members); source corpus and disposable search index are excluded. Restore is only to a missing destination and enforces the stored workspace-root identity; re-home is refused. Archive is unencrypted. Tests: `tests/integration/test_workspace_backup.py`; exact CI runs `41940002906` / `41940002911`. This command remains database-only and is **not full workspace recovery**. |
| Full workspace-owned-files archive and restore | Implemented; Linux/Windows exact-SHA workflows **PASS** on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` (runs `41940002906` / `41940002911`); physical Windows acceptance and encryption remain unqualified. | `database/workspace_archive.py` provides a distinct versioned `.diwa` format, online SQLite snapshot, per-member SHA-256, explicit resource bounds, exclusion/source-reference reports, URL-secret redaction in that report, safe staged extraction, database health/identity verification, and no-replace atomic publication. CLI: `workspace archive` / `workspace restore-archive`. Eligible regular files within the workspace are included; external source files are not copied, while index/cache/logs, VCS/credential/transient files are excluded. Re-homing is refused; the archive is unencrypted. Historical SHA `94d698f...` had test-contract failures and `ff4afdd...` had a separate Windows offscreen UI failure; the latter's root cause remains unproven. Current exact-SHA totals and artifacts are in the Persian mission report. This does not establish production backup policy, encryption, physical Windows acceptance, or external-source recovery. |
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
| Q-01 | Linux/Windows CPython 3.11/3.14 and Ubuntu 26.04 × CPython 3.14 full-suite attribution | **PASS on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`** | Identity-bound reporters passed for Linux run `41940002906` (9/9 jobs) and Windows run `41940002911` (14/14 jobs); both name this exact commit. Check-run totals and all artifact IDs/digests are in the current Persian closure report. |
| Q-02 | Real Qt-enabled UI and lifecycle tests | **PASS in offscreen Qt CI on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`** | Headless local UI tests skip because `libGL.so.1` is absent. Current Linux and Windows offscreen jobs executed DDR/Intake/workbench tests; Windows target test is 29/29 on both runtimes, and stability plus Qt coverage gates passed in runs `41940002906` / `41940002911`. Historical registration/refresh failure was not reproduced; root cause remains unproven. Physical Windows display acceptance remains tracked separately as Q-04. |
| Q-03 | Ubuntu 26.04 with CPython 3.14 | **PASS for this CI matrix lane only** on code/test SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` | Job `126958381778` in run `41940002906`: 2,776 collected, 2,775 passed, 1 skipped, 0 failed/errors; exact-SHA attribution report passed. This confirms this Ubuntu 26.04 × 3.14 CI lane only and does not qualify the product for general release. |
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
