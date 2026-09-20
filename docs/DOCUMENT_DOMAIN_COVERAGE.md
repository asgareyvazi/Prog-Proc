# Document/domain coverage matrix (V2 authority)

**Status:** authoritative source-derived registry and certification index
**As of:** 2026-09-20
**Repository:** `asgareyvazi/Prog-Proc`
**Workspace HEAD inspected:** `b2e76fad64e9b8acac187af45eb9bb8d9d1175c5`
**Contract source:** `src/drilling_intelligence/operations/contracts.py`

This document is not a product wish-list. It records what the checked-out source can safely do. The
`DocumentClassification` enum is the complete row set. The promotion registry is explicit and complete:
`PROMOTION_CONTRACTS` has one entry for every enum value, while only four entries have a domain writer.
A taxonomy member, a classifier result, a knowledge entity, or a search hit is **not** by itself a
permission to write a domain row.

## Capability meanings

| Capability | Meaning in this matrix |
| --- | --- |
| **Classify** | A `TypeSignature` is registered in `classification/taxonomy.py`; `OTHER` is the deterministic fallback, not a positive type-specific match. |
| **Extract** | A configured built-in extractor can retain a normalized artefact for a supported file format. This is format capability, not semantic validation. |
| **Knowledge** | `KnowledgeExtractionService` can derive provenance-carrying facts from stored fields without inventing a domain model. Unsupported fields remain in the artefact and can be skipped/quarantined. |
| **Domain handler** | A named static handler in `contracts.py` is resolved to an existing writer in `VersionPromoter`; there is no plugin or arbitrary-folder fallback. |
| **Reviewable** | The existing `DomainReviewService` can expose the registered document/version/evidence and any derived fact in the requested well scope. This does not mean the document has a domain row. |
| **End-to-end certified** | A deterministic real-file corpus test covers registration, extraction, classification, knowledge/evidence, promotion, scope, provenance, idempotence and the resulting domain rows. |
| **Unsupported** | A version-level promotion outcome, not a discarded source. The stored extraction and any safe knowledge/evidence remain available; no domain writer is entered. |

`EXTRACT_ONLY` and `KNOWLEDGE_SUPPORTED` are intentionally conservative labels. They do not claim a
specific BHA, survey, cost, invoice, procedure, lesson or risk writer exists merely because a repository
or entity vocabulary exists elsewhere in the source tree.

## Complete classification matrix

| `DocumentClassification` | Classify | Extract | Knowledge | Promotion contract / destination | Review surface | Certification / support level | Source evidence and decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `DRILLING_PROGRAM` | yes | yes | yes | `program` -> `drilling_program`, `program_target`, planned `well_section` | yes | **END_TO_END_CERTIFIED** | `contracts.py`; `operations/program.py`; `test_program_promotion.py`; plan/actual tests keep planned and actual columns separate. |
| `DDR` | yes | yes | yes | `report` -> `ddr_report`, `well_operation`, `well_event`, `npt_record`, `problem_occurrence` | yes | **END_TO_END_CERTIFIED** | `operations/promote.py`; generated DOCX DDR in `tests/fixtures/generate.py`; `test_operations_promotion.py`. |
| `MUD_REPORT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | `taxonomy.py`; `knowledge/entities.py` maps mud reports to a safe knowledge subject; no mud-report domain writer exists. |
| `BHA_REPORT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | BHA entity is knowledge-addressable; no deterministic BHA domain table/writer is registered. |
| `BIT_RECORD` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Bit entity is knowledge-addressable; no bit domain writer is registered. |
| `DIRECTIONAL_SURVEY` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Survey/trajectory knowledge vocabulary exists; no survey calculation or authoritative trajectory writer is registered. |
| `CEMENT_REPORT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Cement entity is knowledge-addressable; no cement-job domain writer is registered. |
| `CASING_REPORT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Casing entity is knowledge-addressable; no casing domain writer is registered. |
| `WELL_CONTROL` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Well-control facts can be cited and mapped to safety/event knowledge; no well-control event writer is registered. |
| `LOGGING` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Logging signature exists and facts are source-derived; no log curve/measurement model is registered. |
| `WIRELINE` | no dedicated signature (manual enum value only) | yes | no type-specific contract | none; **unsupported for domain promotion** | yes, evidence only | **EXTRACT_ONLY** | No `TypeSignature` or domain writer is present; do not silently alias it to `LOGGING`. |
| `LWD_MWD` | no dedicated signature (manual enum value only) | yes | no type-specific contract | none; **unsupported for domain promotion** | yes, evidence only | **EXTRACT_ONLY** | No `TypeSignature` or domain writer is present; do not infer telemetry semantics from a filename. |
| `SERVICE_REPORT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Service entity/knowledge mapping exists; no service-job operational writer is registered. |
| `HSE` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | HSE signature and safety-event knowledge mapping exist; no HSE incident domain writer is registered. |
| `NPT` | yes | yes | yes | `report` -> explicit NPT/lost-hours table rows and operational records | yes | **END_TO_END_CERTIFIED** | `operations/repository.py` report set; header-gated `find_npt_tables`; generated CSV and production promotion tests. |
| `COST` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | `engineering/costs.py` supports governed/manual cost records, but no source-to-cost writer is registered. |
| `INVOICE` | no dedicated signature (manual enum value only) | yes | no type-specific contract | none; **unsupported for domain promotion** | yes, evidence only | **EXTRACT_ONLY** | No invoice parser, payable model, or static writer exists; monetary text must not become a cost row. |
| `TIME_BREAKDOWN` | yes | yes | yes | `report` -> activity rows and explicitly coded NPT rows | yes | **DOMAIN_PROMOTABLE** | Handler is header/code gated. It is not marked end-to-end certified because the V2 corpus has not yet certified a standalone `TIME_BREAKDOWN` classification. |
| `EOWR` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | EOWR is a retrospective evidence/knowledge source; no source-owned replacement writer is registered. |
| `PROCEDURE` | yes | yes | yes | none; **unsupported for source promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Procedure revisions are supported through explicit human/domain repository APIs; ingestion does not turn text into a procedure row. |
| `STANDARD` | yes | yes | yes | none; **unsupported for source promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Standards are reference/knowledge evidence; no automatic compliance or requirement writer is registered. |
| `CONTRACT` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Commercial text is retained and citable; no contract/obligation writer is registered. |
| `TECHNICAL_REFERENCE` | yes | yes | yes | none; **unsupported for domain promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Reference facts are safe only with provenance; no engineering value is promoted from generic prose. |
| `BOOK` | no dedicated signature (manual enum value only) | yes | no type-specific contract | none; **unsupported for domain promotion** | yes, evidence only | **EXTRACT_ONLY** | No book signature or domain model; a technical book must not be treated as a standard/reference without evidence. |
| `LESSON_LEARNED` | yes | yes | yes | none; **unsupported for source promotion** | yes, evidence/facts only | **KNOWLEDGE_SUPPORTED** | Lesson repository/actions are explicit and human-governed; no automatic lesson row is created from classification alone. |
| `OTHER` | fallback only | yes | no type-specific contract | none; **unsupported for domain promotion** | yes, evidence only | **EXTRACT_ONLY** | `OTHER` is the no-match/low-confidence fallback. It is never an invitation to inspect every table as NPT. |

### Certified versus promotable

The table deliberately separates `DOMAIN_PROMOTABLE` from `END_TO_END_CERTIFIED`. The four static
handler entries are:

```text
DRILLING_PROGRAM -> program
DDR              -> report
NPT              -> report
TIME_BREAKDOWN   -> report
```

Only the first three are certified against the current deterministic real-file corpus. The `TIME_BREAKDOWN`
handler is restricted to tables with an activity header and an explicit duration column; an NPT row in a
breakdown needs an explicit NPT code. The handler never promotes narrative text, a total row, an unparseable
duration, an unknown well, or a row with no table provenance.

## Promotion outcome contract

`PromotionResult.outcome` and batch `summary["outcomes"]` use these exclusive version-level values:

- `ELIGIBLE` — a registered handler accepted the version but no new/unchanged row was needed;
- `PROMOTED` — at least one new domain row was written;
- `UNCHANGED` — all supported identities were already present;
- `UNSUPPORTED` — no handler exists for this classification;
- `AMBIGUOUS` — the handler refused to choose among candidate sections/identities;
- `MISSING_ARTEFACT` — no stored extraction artefact exists;
- `MISSING_WELL` — the required subject linkage is absent;
- `MISSING_PROVENANCE` — a candidate row/field cannot point to its source;
- `INVALID_FIELDS` — required values/units/field quality are not usable;
- `CONFLICT` — a source re-run differs from a stored row, which is left untouched;
- `ERROR` — an unexpected or integrity failure prevented the contract from completing.

`records promote --include-unsupported` is the forensic batch mode. The default batch visits only the
four handler classes; the option visits every current extracted version and reports explicit unsupported
outcomes instead of omitting evidence-only documents. CLI JSON contains `contract_id`, `eligible`,
`outcome`, `eligibility`, `outcomes`, `skipped`, and row-level `counts`. The Review Workbench exposes the
same distinction on document rows through `promotion_contract`, `promotion_eligibility`, and
`promotion_unsupported`/`promotion_not_certified` flags.

## Non-authoritative surfaces

Search/index chunks, classifier scores, filenames, folder names, AI suggestions, and a source document's
approval stamp are discovery/evidence signals only. Authoritative promotion reads the stored extraction
artefact, checks static class/handler eligibility, field quality/units/linkage and provenance, and writes
through the existing repositories. Review, indexing, stale detection and source navigation never invoke
the NPT executor. The only executable calculation remains the explicit NPT V1 roll-up action.

## How to inspect the registry

```python
from drilling_intelligence.operations.contracts import contract_registry

for contract in contract_registry():
    print(contract.to_dict())
```

The import-time completeness guard makes a new enum member fail fast until this registry and this
matrix are deliberately updated. The registry is the machine-readable authority; this document is the
human-readable forensic explanation of its source evidence and certification boundary.
