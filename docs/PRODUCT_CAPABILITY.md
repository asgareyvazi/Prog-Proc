# Product capability inventory

**Status:** source-derived inventory and finite milestone list
**As of:** 2026-10-17
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
| Incremental scan and ingest | Implemented and end-to-end tested | `ingestion/pipeline.py` (`mode="incremental"`), `tests/integration/test_ingestion_pipeline.py` |
| PDF, XLSX, DOCX, text, and CSV extraction | Implemented and end-to-end tested | `extraction/pdf_text.py`, `excel.py`, `docx.py`, `text.py`, generated-corpus tests |
| MinerU for scans, with built-in fallback | Implemented with limited coverage | `extraction/registry.py` wires `MinerUExtractor`; `tests/integration/test_mineru_boundary.py`. A missing runtime stays a fallback. This checkout did not run MinerU. |
| Deterministic classification | Implemented and end-to-end tested | `classification/taxonomy.py`, corpus tests. No model call. |
| Registry, versions, supersession, audit | Implemented and end-to-end tested | `documents/registry.py`, `documents/repository.py`, document invariant tests |
| Knowledge facts, relations, conflicts, human resolution | Implemented and end-to-end tested | `knowledge/service.py`, `tests/unit/test_knowledge_facts.py`, conflict integration tests |
| Domain promotion for the certified classes | Implemented and end-to-end tested | `operations/contracts.py` and the coverage matrix. 13 end-to-end certified, 8 knowledge-supported, 5 extract-only. |
| DDR, NPT, and operational events | Implemented and end-to-end tested | `operations/promote.py`, forensic corpus tests |
| Engineering records and the NPT roll-up | Implemented and end-to-end tested | `engineering/repository.py`, `engineering/costs.py` persists source costs and does not convert currency. NPT roll-up is the only executable method. |
| Search, retrieval, evidence packages | Implemented and end-to-end tested | `search/service.py`, `retrieval/service.py`, `evidence/service.py`, forensic tests |
| Citation verification | Implemented and end-to-end tested | `evidence/verify.py`, `reporting/audit.py`. Opt-in. A match is not full coverage. |
| Field intelligence, timeline, patterns, recommendations | Implemented and end-to-end tested | `intelligence/field.py`, `timeline.py`, `patterns.py`, `tests/integration/test_field_intelligence.py` |
| Decision and comparison packs | Implemented and end-to-end tested | `intelligence/decision.py`, `comparison.py`, V7.5 and V7.6 tests |
| Analyst question catalog | Implemented and end-to-end tested | `intelligence/analyst.py`. Fixed question ids. No natural-language parser and no model. |
| Engineering report and source lineage | Implemented and end-to-end tested | `reporting/service.py`, `lineage.py`, report tests. Renderers issue no SQL. |
| Desktop Review Workbench | Implemented with limited coverage | `ui/main_window.py`, `ui/lifetime.py`, Qt CI jobs. Headless pytest skips the UI tests when PySide6 is absent. That skip is not a pass. |
| SQLite lifecycle and migrations | Implemented and end-to-end tested | Alembic head `0019`. `database/engine.py` binds datetimes on the SQLite engine only. |
| Package, CLI, and release smoke | Implemented and end-to-end tested | `pyproject.toml` scripts `drillintel` and `drillintel-ui`, `tools/release_smoke.py`, clean-install CI job |
| Ollama probe, completion, and embedding | Implemented with limited coverage | `integrations/ai/ollama.py`, `tests/unit/test_ai_provider.py`. Not called by extraction, promotion, search, or citation. A live Ollama is opt-in via `DRILLINTEL_AI_LIVE=1` and is not a CI pass. |
| OpenAI-compatible provider | Architecture or interface only | `AiSettings.provider` accepts the name. No adapter sends a request. |
| Vector store and semantic ranking | Architecture or interface only | `UnavailableVectorStore` reports the gap. `[search]` vector keys are parsed and unused (`configs/development.toml`). |
| Skills | Architecture or interface only | `database/models.py` `Skill` and `SkillVersion`. No service reads them. |
| Additional calculation engines | Planned | README and ADR policy. NPT roll-up V1 is the only registered executable method. |
| Risk scoring, cost/AFE engine, plan-vs-actual dashboards | Deliberately excluded | `docs/DOMAIN.md`. Cost rows keep source currency. Comparison refuses unit conversion. |
| Trajectory mathematics | Planned | ADR names a later `wellpathpy` wrapper. Survey stations are stored as reported. No interpolation in the directional-survey contract. |
| CPython 3.14 on Ubuntu 26.04 | Unknown as a certified runtime | The Ubuntu 26.04 CI lane is CPython 3.11. No job has certified 3.14 on that image. |

## This milestone

V8.11 implements the Ollama capability contract because the audit found the AI
seam still architecture-only, and ADR-0005 already chose the protocol, the
transport, and the local provider. It does not invent a calculation, a risk
scale, or a vector index.

Exit criteria for this milestone:

- `provider = none` and `openai-compatible` send no request.
- A successful probe requires the configured model to be listed.
- `doctor` does not open a socket and does not report configuration as availability.
- Completion and embedding results are non-authoritative.
- The engineering packages listed in ADR-51 do not import the adapter.
- Alembic head remains `0019`.

## Finite milestones after this one

These are the next product gaps. A later mission should take one of them, not
all of them.

| Milestone | What is missing | Why it waits | Acceptance test |
| --- | --- | --- | --- |
| M2. OpenAI-compatible adapter | A real HTTP adapter for the name already accepted in settings | The ADR chose Ollama. The cloud URL, auth header, and whether `ai status` may leave the machine are not specified. | Mocked 401, 404, and 200 tests; no request when the env key is absent; no key in logs; engineering packages still do not import it. |
| M3. Non-authoritative embedding sidecar | A place to store vectors and a similarity rule | ADR-0005 rejected Chroma. Dimension, metric, and whether a sidecar may affect retrieval are unspecified. | A rebuild can drop the sidecar; retrieval answers do not change when it is absent; no domain table gains a vector column without a migration test. |
| M4. Skills runtime | Behavior for the `skill` tables | The tables have no service and no operator contract. | A skill cannot become a calculation or a promoted row unless a written contract says so. |
| M5. One additional calculation method | Any engine besides NPT roll-up | The method, units, and failure mode are not specified. | The method id, inputs, and refusal cases exist before the code, and review does not recalculate. |
| M6. One knowledge-supported document class | A domain writer for a class the matrix leaves without a handler | Each admission so far required a source-shape contract. Inventing one is out of scope. | The coverage matrix moves that class only after a forensic corpus test. |

## Deliberately not scheduled

Risk scores, currency conversion, AFE rules, plan-versus-actual dashboards, and
inferred survey geometry stay excluded until the project owner specifies the
rule. Their absence is not a defect in this inventory.
