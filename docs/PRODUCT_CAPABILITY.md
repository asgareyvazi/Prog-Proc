# Product capability inventory
## Snapshot جاری — 2026-11-22 (UTC)، SHA نوک شاخه `arena/5957b76d-prog-proc` = `48040f81a895d4b7de372a03aa58e1c7214816e6`


- به‌روزرسانی ۲۰۲۶-۱۰-۲۰ (این اسنپ‌شات): resolved نسخهٔ canonical از طریق pointer (‌`current_version`)؛ schema رویداد واحد ‌`extraction_disposition`‌ برای Program و Procedure با policy تصمیم مجدد؛ پنل منبع واقعی با re-verify هش فیزیکی (‌`read_source_excerpt`‌) در UI؛ تست‌های اجباری مسیر fallback انتشار (renameat2 NOREPLACE / fail-closed)؛ و regression تست manifest شارد run-scoped. tip فعلی ‌`48040f81a895d4b7de372a03aa58e1c7214816e6`‌؛ گزارش مأموریت به فارسی در ‌`docs/mission-reports/`‌ و SHA نهایی CI در گزارش پایانی ثبت می‌شود.
- Baseline تأییدشدهٔ قبلی: `2dc7ddde7d595729f98e67485449514a277b07fc` — Linux run `44279805151` و Windows run `44279805136` هر دو success (2,951 collected؛ 0 failed)، و historical replay run `44299260397` (job `134784363027`) success با artifact `14425148074`؛ run تاریخی `43695755434` روی `114173a4` همچنان failure ثبت شده است.
- Migrationهای `0027` و `0028` (لینک دقیق Program↔Procedure revision با NONE/LINKED/STALE و بدون auto-relink) در head هستند؛ head Alembic `0028` است.
- قابلیت‌های headless: authoring service کامل (sections/targets/steps، freshness token، validate، submit/review/decide، revise، compare)، templateهای فقط‌ساختاری، `draft-from-document` (استخراج واقعی متن ساده با re-verify هش فیزیکی، resolve نسخهٔ canonical، و event dispositions مشترک Program/Procedure)، و export JSON/HTML/DOCX/PDF با watermark DRAFT، fidelity کامل، unitهای ذخیره‌شده، redaction opt-in (`path-redaction-v1`) و انتشار race-safe no-clobber.
- Qt: صفحات **Program Builder** و **Procedure Builder** کامل شدند (`ui/authoring_pages.py`) — ویرایشگر Program target با همهٔ champها و واحدها و provenance، ویرایشگر کامل step با equipment/preconditions/پارامترهای JSON/expected result/evidence/safety/contingency، و دکمه‌های lifecycle (validate/submit/review/approve/reject/revise/compare) همگی از طریق کنترل‌های Qt؛ ۵ تست offscreen UI + ۳۶ تست workbench سبز هستند. Screenshots فیزیکی به‌علت فقدان libGL در host هنوز NOT RUN است.
- `out/performance.json` دست‌نخورده (همان blob.tracked remoto؛ بدون تغییر).
- CI exact-SHA برای SHA جاری (`eafe467`) در زمان snapshot در حال اجراست (Linux run `44875347194`, Windows run `44875347155`)؛ tip قبلی `f73f5d21` با Linux run `44360206371`, Windows run `44360206575`, و replay run `44374986517` (success) qualifای شده بود.证据 نهایی در گزارش پایانی مأموریت ثبت می‌شود.

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
| Operator intake, preview, governed disposition, and registry registration | Implemented in services/CLI/UI; Linux and Windows exact-SHA Qt-enabled jobs **PASS** on SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5`; Qt UI is **SKIPPED locally** because `libGL.so.1` is absent | `review/intake.py`, `documents/registry.py`, migrations `0024`/`0025`, `tests/integration/test_intake_registration.py`, `tests/integration/test_intake_e2e.py`, `tests/ui/test_intake_workbench.py`, ADR 004. Preview writes no registry/domain row; registration creates document evidence only and does not promote. Exact Linux run `42381352913` (9/9 jobs) and Windows run `42381353106` (14/14 jobs) passed; Windows UI was 29/29 on both runtimes. A historical registration/refresh failure was not reproduced and its root cause is not established. |
| Daily DDR manual authoring workspace | Service lifecycle, independent-process write-race tests, and exact-SHA Qt-enabled Linux/Windows jobs **PASS** on implementation SHA `075d5919ebd8a5e600e3c806b73bc560203f52c5`; local comparison rendering **SKIPPED** because PySide6 is unavailable | `operations/ddr.py`, `operations/ddr_comparison.py`, migration `0026`, `review/actions.py`, `ui/main_window.py`, `tests/integration/test_ddr_authoring.py`, `tests/unit/test_ddr_comparison.py`, `tests/ui/test_workbench.py`. Submit keeps report/operations CANDIDATE. Comparison is read-only, preserves authored text/order, identifies predecessor/successor and activity/field deltas, and does not imply approval. Distinct actor strings are not authenticated identities. Exact Linux run `42466023039` and Windows run `42466023072`; local Qt rendering was not run. |
| Headless manual DDR CLI | Implemented as a thin service adapter; exact-SHA Linux and Windows full suites **PASS** on implementation SHA `075d5919ebd8a5e600e3c806b73bc560203f52c5` | `cli/app.py` provides `ddr create/list/show/actions/save/submit/revise/decide`; strict bounded JSON rejects duplicate/unknown keys; save requires a complete snapshot and stale token; review is executed only by `ReviewActionService`, including report `revision_number` freshness checks. `tests/integration/test_ddr_cli.py`, `tests/unit/test_cli_surface.py`, `tests/snapshots/cli_surface.txt`. The CLI does not authenticate actor strings, promote imported files, derive engineering values, or auto-confirm operations. Exact Linux run `42466023039`; Windows run `42466023072`. |
| Headless Program and Procedure authoring slice | **Implemented locally; not yet CI-qualified on the final correction SHA.** SQLite migration `0027`, revision-owned Program sections/targets and Procedure steps, strict bounded JSON CLI, optimistic stale-write guards, structural review gate, review attribution/history, revision copy and read-only comparison are covered by the cited integration/snapshot suites. Exact SHA `8add9067…` failed workflow `44031899454` because old migration expectations and pins were stale; local correction selection passed 116 tests with 1 skipped (not a pass). No Qt editor, templates, controlled DOCX/PDF export, engineering calculation or exact-revision Program→Procedure operational link is claimed. See `docs/ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md` and `docs/PROGRAM_PROCEDURE_AUTHORING_CLI.md`. |
| `drillintel config validate` and `drillintel mineru validate` | **Implemented, boundary-tested on SHA `075d5919…`; exact-SHA Linux/Windows suites PASS.** Configuration validation checks effective local settings and environment overrides without service probes or database/workspace access. MinerU validation distinguishes CLI readiness, unchecked local models, and OCR qualification; optional inference is synthetic-fixture-only and local-only. The actual mission host returned `NOT_READY`, model `NOT_CHECKED`, OCR `NOT_RUN`, metrics `null`. |
| Read-only DDR revision comparison and export | **Implemented; exact-SHA Linux and Windows workflows PASS on `82f1ce16b2a2e9819c9d4e6ab773f86c8c1cbbda`** (Linux run `43707382760`, final attempt 2; Windows `43707382725`, attempt 1). Linux attempt 1 had one workbench UI-test failure; its same-SHA rerun passed. The local Qt UI export test is **NOT RUN** because `libGL.so.1` is absent. Full-fidelity JSON/Markdown remains the default. `--redact-source-paths` and the explicit UI privacy choice create a shareable copy, redact only documented exact provenance path keys, record policy/schema, and leave authored text/comparison/DB/audit state unchanged. Output uses same-directory temporary files, no-clobber default, and explicit overwrite. Final run's shard-reconciliation job passed, but raw artifact ZIP payload inspection and the required offline replay of historical failed run `43695755434` remain **NOT RUN** because artifact redirects are outside the sandbox egress allowlist; overall mission qualification remains **NOT QUALIFIED** pending that evidence. See `docs/DDR_COMPARISON_OPERATOR_GUIDE.md` and the current mission report. |
| Incremental scan and ingest | Implemented and end-to-end tested | `ingestion/pipeline.py` (`mode="incremental"`), `tests/integration/test_ingestion_pipeline.py` |
| PDF, XLSX, DOCX, text, and CSV extraction | Implemented and end-to-end tested | `extraction/pdf_text.py`, `excel.py`, `docx.py`, `text.py`, generated-corpus tests |
| Optional MinerU 4 extraction for complex/scanned PDFs | **Implemented; local synthetic-runtime smoke passed on a prior SHA; OCR accuracy remains NOT QUALIFIED.** This mission adds guarded transport and a local validation command; its host has no MinerU runtime, so runtime readiness is **NOT READY** and inference is **NOT RUN** here. | `integrations/mineru/client.py` invokes external `mineru-kit parse` and normalizes `docvortex.middle`; no runtime/model is in core dependencies. Default model source is `local` to prevent implicit model fetches; network-backed sources require explicit configuration. CLI readiness does not inspect models; opt-in synthetic inference emits fixture-scoped CER/WER only after real local inference. Legacy HTTP accepts the supported multipart/JSON contract and rejects attachment/ZIP responses. Parser text remains extraction evidence and never auto-promotes a record. See README and the current Persian mission report. |
| Deterministic classification | Implemented and end-to-end tested | `classification/taxonomy.py`, corpus tests. No model call. |
| Registry, versions, supersession, audit | Implemented and end-to-end tested | `documents/registry.py`, `documents/repository.py`, document invariant tests |
| Lesson code identity, lifecycle, and source revisions | Implemented and regression-tested; source removal/rename semantics remain owner-gated | `lessons/repository.py`, `operations/promote.py`, migrations `0020`–`0023`; canonical strip + Unicode casefold global uniqueness is enforced in repository/promotion and by the current-row partial unique index. Supersession preserves predecessor `prior_status`; a new successor starts DRAFT without inherited approval. Focused lesson/exception tests passed 95/95; C1 full-suite exact-SHA CI also passed on Linux run `42381352913` and Windows run `42381353106`. Deletion or code change in a source without explicit successor requires owner policy; do not infer automatic disposition. |
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
| Desktop Review Workbench | Linux and Windows Qt-enabled CI jobs **PASS** on code/test SHA `075d5919ebd8a5e600e3c806b73bc560203f52c5`; physical desktop acceptance remains open | `ui/main_window.py` uses existing domain read services and review actions; authoring mutations remain off the GUI thread. Linux run `42466023039` and Windows run `42466023072` passed; Windows Qt offscreen tests were 29/29 for each Python runtime, with all six stability lanes 15/15. Local UI tests skip because PySide6 is absent. Offscreen CI is not physical Windows display acceptance. |
| SQLite lifecycle and migrations | SQLite remains qualified for its documented local single-user use; migration `0027` and final-SHA regression qualification are **pending**. | Alembic migrations `0025`–`0027`, `database/migrations.py`, and migration/integrity tests. Historical evidence through `0026` remains SHA-scoped to its cited runs. PostgreSQL remains **NOT QUALIFIED**. |
| SQLite storage health / doctor preflight | Implemented; exact-SHA Linux/Windows suite evidence **PASS** on C1 code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | `database/sqlite_health.py` runs read-only integrity/FK/sidecar checks before bootstrap and rechecks after database creation/upgrade; a failed preflight blocks domain/search health claims. No automatic repair. Tests: `tests/unit/test_sqlite_health.py`, `tests/integration/test_doctor_storage_health.py`; exact CI runs `42381352913` / `42381353106`. |
| SQLite database snapshot backup and restore | Implemented with bounded resource and publication safeguards; exact-SHA Linux/Windows suite evidence **PASS** on C1 code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | `database/backup.py` uses SQLite online backup, SHA-256 manifest, schema revision, integrity/FK checks and no-overwrite publication. It includes only `workspace.toml` plus DB snapshot (8 GiB DB, 16 MiB archive overhead, 1 MiB metadata, exactly three archive members); source corpus and disposable search index are excluded. Restore is only to a missing destination and enforces the stored workspace-root identity; re-home is refused. Archive is unencrypted. Tests: `tests/integration/test_workspace_backup.py`; exact CI runs `42381352913` / `42381353106`. This command remains database-only and is **not full workspace recovery**. |
| Full workspace-owned-files archive and restore | Implemented; Linux/Windows exact-SHA workflows **PASS** on C1 code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` (runs `42381352913` / `42381353106`); physical Windows acceptance and encryption remain unqualified. | `database/workspace_archive.py` provides a distinct versioned `.diwa` format, online SQLite snapshot, per-member SHA-256, explicit resource bounds, exclusion/source-reference reports, URL-secret redaction in that report, safe staged extraction, database health/identity verification, and no-replace atomic publication. CLI: `workspace archive` / `workspace restore-archive`. Eligible regular files within the workspace are included; external source files are not copied, while index/cache/logs, VCS/credential/transient files are excluded. Re-homing is refused; the archive is unencrypted. Historical SHA `94d698f...` had test-contract failures and `ff4afdd...` had a separate Windows offscreen UI failure; the latter's root cause remains unproven. Totals for `b3163bf...` belong only to runs `41940002906` / `41940002911`; the 2026-11-04 closure report is historical. This does not establish production backup policy, encryption, physical Windows acceptance, or external-source recovery. |
| Package, CLI, and release smoke | Implemented and end-to-end tested | `pyproject.toml` scripts `drillintel` and `drillintel-ui`, `tools/release_smoke.py`, clean-install CI job |
| Ollama probe, completion, and embedding | Implemented with limited coverage | `integrations/ai/ollama.py`, `tests/unit/test_ai_provider.py`. Success requires a finite vector per input and an exact model name. Not called by extraction, promotion, search, or citation. A live Ollama is opt-in via `DRILLINTEL_AI_LIVE=1` and is not a CI pass. |
| OpenAI-compatible provider | Architecture or interface only | `AiSettings.provider` accepts the name. No adapter sends a request. |
| Vector store and semantic ranking | Architecture or interface only | `UnavailableVectorStore` reports the gap. `[search]` vector keys are parsed and unused (`configs/development.toml`). |
| Skills | Architecture or interface only | `database/models.py` `Skill` and `SkillVersion`. No service reads them. |
| Additional calculation engines | Planned | README and ADR policy. NPT roll-up V1 is the only registered executable method. |
| Risk scoring, cost/AFE engine, plan-vs-actual dashboards | Deliberately excluded | `docs/DOMAIN.md`. Cost rows keep source currency. Comparison refuses unit conversion. |
| Trajectory mathematics | Planned | ADR names a later `wellpathpy` wrapper. Survey stations are stored as reported. No interpolation in the directional-survey contract. |
| Ubuntu 26.04 × CPython 3.14 CI lane | **PASS on C1 code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` (matrix lane only)** | Job `128429443547`, Linux run `42381352913`: 2,795 collected, 2,794 passed, 1 skipped, 0 failed/errors; exact-SHA attribution report passed. This confirms this Ubuntu 26.04 × 3.14 CI lane only and does not claim general release qualification or physical Windows acceptance. |
| Windows install and offscreen UI | CPython 3.11/3.14 suite, clean install, Qt-enabled UI/stability and PowerShell run-card tests **PASS** on C1 code SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | `tools/venv_executables.py`, `tools/windows_pilot.ps1`, `.github/workflows/ci-windows.yml` on `windows-2022`, run `42381353106` (14/14 jobs success). Offscreen CI and disposable pilot are not manual desktop acceptance; physical display acceptance remains **NOT RUN**. |

## Closure blockers and unresolved defects — single register

This section is the closure ledger for the capability inventory above. It distinguishes a product defect from absent qualification evidence or an owner decision; historical reports are not closure evidence for a later SHA.

| ID | Item | State | Closure evidence / owner action |
| --- | --- | --- | --- |
| Q-01 | Linux/Windows CPython 3.11/3.14 and Ubuntu 26.04 × CPython 3.14 full-suite attribution | **PASS on Linux and Windows exact-SHA runs** for code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | Linux run `42381352913` (9/9 jobs) passed. CPython 3.11/3.14 each collected 2,795: 2,734 passed, 61 skipped, 0 failed; Ubuntu 26.04/3.14 collected 2,795: 2,794 passed, 1 skipped, 0 failed. Windows run `42381353106` (14/14 jobs) passed: CPython 3.11/3.14 each collected 2,795 (2,732 passed, 63 skipped, 0 failed). Artifact/log downloads returned EOF, so do not infer unreported skip reasons. |
| Q-02 | Real Qt-enabled UI and lifecycle tests | **Linux/Windows offscreen jobs PASS** on code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | Linux workbench offscreen jobs, Qt coverage, and stability gates passed in run `42381352913`. Windows UI passed 29/29 on both Python versions, and all six stability lanes completed 15/15 in run `42381353106`; the exact-SHA Windows workflow is complete. Headless local UI skips because `libGL.so.1` is absent. Historical registration/refresh failure was not reproduced; root cause remains unproven. Physical Windows display acceptance remains tracked separately as Q-04. |
| Q-03 | Ubuntu 26.04 with CPython 3.14 | **PASS for this CI matrix lane only** on code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` | Job `128429443547` in Linux run `42381352913`: 2,795 collected, 2,794 passed, 1 skipped, 0 failed/errors; exact-SHA attribution report passed. This confirms this Ubuntu 26.04 × 3.14 CI lane only and does not qualify the product for general release. |
| Q-04 | Physical Windows desktop display acceptance | **NOT RUN** | Requires a human operator on a real Windows desktop; offscreen CI and the disposable pilot are not substitutes. |
| Q-05 | PostgreSQL backend | **NOT QUALIFIED** | SQLite remains the only qualified backend. A product target and a real PostgreSQL service/migration/integration matrix are prerequisites; no assumption or skipped test closes this item. |
| O-01 | M2–M5: external AI adapter, vector sidecar, Skills runtime, additional engineering method | **OWNER-GATED** | Remain outside this completion scope until the product owner approves contracts, data boundaries, security and acceptance cases. |
| O-02 | Trajectory/directional calculations | **OWNER-GATED / calculations disabled** | Survey station storage may remain; no calculation is enabled without a reviewed method, units, assumptions, reference and refusal contract plus independent verification. |
| O-03 | Lesson source removal or `code` change without explicit successor | **OWNER-GATED / OPEN** | Current code has global casefold identity and chain validation, but no owner-approved removal/rename disposition. Isolated audit rejected an orphaned chain; owner must specify predecessor status, successor/code identity, reuse, approval and audit semantics before an implementation. See `PRODUCT_DECISIONS_REQUIRED.md`. |

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
