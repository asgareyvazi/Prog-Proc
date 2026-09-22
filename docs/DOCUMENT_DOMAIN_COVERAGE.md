# Document/domain coverage matrix (V3 authority)

**Status:** authoritative source-derived registry and certification index
**As of:** 2026-09-20 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0b7fd-prog-proc`
**Inspected HEAD before final validation:** `6af4e42cdcf43011c9789e829826296082062034`
**Contract source:** `src/drilling_intelligence/operations/contracts.py`
**Forensic corpus:** `tests/golden_corpus/manifest.json`

This matrix is reconstructed from the checked-out enum, taxonomy, stored extraction paths, model tables,
repository methods, tests and executable behaviour. It is not a roadmap. A classification, knowledge
entity, filename, folder, search hit or approval stamp is not permission to write a domain row.

## Exact registry counts

| Measure | Count | Authority |
| --- | ---: | --- |
| `DocumentClassification` members | **26** | `core/enums.py` |
| explicit static contracts | **26** | `contract_registry()` import-time completeness guard |
| domain handlers | **5** | `program`, `report`, `mud_report` (DDR/NPT/TIME_BREAKDOWN share the named `report` handler) |
| `END_TO_END_CERTIFIED` | **4** | drilling program, DDR, NPT, mud report |
| `DOMAIN_PROMOTABLE` but not end-to-end certified | **1** | time breakdown |
| `KNOWLEDGE_SUPPORTED`, no domain writer | **16** | explicit deny-by-no-handler registry entries |
| `EXTRACT_ONLY`, no type-specific knowledge/domain contract | **5** | explicit deny-by-no-handler registry entries |
| deterministic V3 corpus cases | **12** | `build_v3_forensic_corpus()` and `test_v3_forensic_corpus.py` |

The five handler entries preserve the V2 contract IDs for existing writers. `MUD_REPORT` is the only
new writer and is explicitly revisioned `v3`; no existing contract ID was silently changed.

## Capability meanings

| Capability | Meaning in this matrix |
| --- | --- |
| **Classify** | A deterministic `TypeSignature` is present in `classification/taxonomy.py`; `OTHER` is the no-match/degraded fallback. |
| **Extract** | A built-in router/extractor can retain a stored artefact for the source format. It is not semantic validation. |
| **Knowledge** | Stored fields can feed the existing provenance-carrying knowledge boundary. This does not create a domain row. |
| **Domain handler** | A named static handler is resolved by `VersionPromoter`; no plugin, arbitrary folder or filename fallback exists. |
| **Reviewable** | Existing review can expose the document/version, extraction diagnostics, provenance and any admitted structured rows in scope. |
| **End-to-end certified** | Real generated files cover registration, extraction, classification, promotion, scope, provenance, identity, replacement/idempotence and read surfaces. |
| **Unsupported** | A successful safety outcome: artefact/evidence remains available, but no authoritative domain writer is entered. |

## Complete 26-class forensic matrix

`Yes` in the **classify** column means a taxonomy signature exists; `manual/fallback` means the enum
can be stored but the classifier has no dedicated positive signature. **Knowledge-supported** does not
mean a specialized writer exists. **Target models** are the only tables a contract is allowed to write.

| # | Classification | Classify | Extract | Knowledge | Static level | Handler / target | Review/search | V3 disposition and source decision |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `DRILLING_PROGRAM` | yes | yes | yes | `END_TO_END_CERTIFIED` | `program` -> `drilling_program`, `program_target`, planned `well_section` | yes | Certified; planned values and actual section values stay distinct. |
| 2 | `DDR` | yes | yes | yes | `END_TO_END_CERTIFIED` | `report` -> `ddr_report`, `well_operation`, `well_event`, `npt_record`, `problem_occurrence` | yes | Certified; typed/table rows only, not narrative guessing. |
| 3 | `MUD_REPORT` | yes | yes | yes | `END_TO_END_CERTIFIED` | `mud_report` -> `mud_report`, `mud_measurement` | yes | **Admitted V3.** Summary and repeated daily values retain source units, sample identity and locators. |
| 4 | `BHA_REPORT` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no BHA authoritative table or deterministic writer is registered. |
| 5 | `BIT_RECORD` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no bit-run writer is registered. |
| 6 | `DIRECTIONAL_SURVEY` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no trajectory/survey writer or calculation contract is registered. |
| 7 | `CEMENT_REPORT` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no cement-job writer is registered. |
| 8 | `CASING_REPORT` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no casing-run writer is registered. |
| 9 | `WELL_CONTROL` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no automatic well-control event writer is registered. |
| 10 | `LOGGING` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no log-curve/measurement writer is registered. |
| 11 | `WIRELINE` | manual/none | yes | no type-specific contract | `EXTRACT_ONLY` | no handler | evidence | No dedicated signature; never silently aliased to logging. |
| 12 | `LWD_MWD` | manual/none | yes | no type-specific contract | `EXTRACT_ONLY` | no handler | evidence | No dedicated signature; telemetry semantics are not inferred. |
| 13 | `SERVICE_REPORT` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no service-job writer is registered. |
| 14 | `HSE` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retained/citable; no incident/event writer is registered. |
| 15 | `NPT` | yes | yes | yes | `END_TO_END_CERTIFIED` | `report` -> operational typed rows | yes | Certified; explicit NPT headers/codes and units gate promotion. |
| 16 | `COST` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no source handler | yes | Manual/governed cost APIs exist; source text is not made into a cost row. |
| 17 | `INVOICE` | manual/none | yes | no type-specific contract | `EXTRACT_ONLY` | no handler | evidence | No payable parser or writer; monetary prose cannot become cost. |
| 18 | `TIME_BREAKDOWN` | yes | yes | yes | `DOMAIN_PROMOTABLE` | `report` -> `ddr_report`, `well_operation`, `npt_record` | yes | Restricted writer; requires explicit activity and duration columns. No standalone V3 certification fixture. |
| 19 | `EOWR` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Retrospective evidence only; no source-owned replacement writer. |
| 20 | `PROCEDURE` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no source handler | yes | Human/domain procedure APIs remain explicit; ingestion does not author a procedure. |
| 21 | `STANDARD` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Reference evidence only; no compliance/requirement writer. |
| 22 | `CONTRACT` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Commercial evidence only; no obligation writer. |
| 23 | `TECHNICAL_REFERENCE` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no handler | yes | Reference facts remain provenance-bound; no engineering value promotion. |
| 24 | `BOOK` | manual/none | yes | no type-specific contract | `EXTRACT_ONLY` | no handler | evidence | No book signature or domain model; no silent reference alias. |
| 25 | `LESSON_LEARNED` | yes | yes | yes | `KNOWLEDGE_SUPPORTED` | no source handler | yes | Human lesson repository/actions remain explicit; classification does not auto-create a lesson. |
| 26 | `OTHER` | fallback only | yes/degraded | no type-specific contract | `EXTRACT_ONLY` | no handler | evidence | Safe fallback; never interpreted as NPT/mud/BHA/etc. |

### Static contracts and required evidence

```text
DRILLING_PROGRAM -> document:DRILLING_PROGRAM:promotion:v2 -> program
DDR              -> document:DDR:promotion:v2              -> report
NPT              -> document:NPT:promotion:v2              -> report
TIME_BREAKDOWN   -> document:TIME_BREAKDOWN:promotion:v2  -> report
MUD_REPORT       -> document:MUD_REPORT:promotion:v3       -> mud_report
```

The mud contract requires a stored extraction, a recognised summary label/value/unit shape, a repeated
sample-labelled daily table, preserved source units, row/table provenance, a linked well and explicit
section resolution. A missing or ambiguous section is represented by `section_id = NULL`; it is never
filled from MD/TVD, row order, filename, nearest depth or a UI selection.

## Outcome taxonomy

The version-level outcomes are exclusive and intentionally distinguish denials from empty success:

- `ELIGIBLE`, `PROMOTED`, `UNCHANGED`;
- `UNSUPPORTED`, `AMBIGUOUS`, `MISSING_ARTEFACT`, `MISSING_WELL`, `MISSING_PROVENANCE`;
- `INVALID_FIELDS`, `CONFLICT`, `ERROR`.

`records promote --include-unsupported` is the explicit forensic batch mode. It visits all current
extracted versions and reports static denials; the default visits only admitted handlers. JSON includes
contract ID, eligibility, outcome, row-level counts, skip reasons and details.

## Non-authoritative boundaries

- Search/index is disposable discovery. Retrieval re-reads authoritative structured rows in bounded
  batches; evidence packages are composed only from retrieval.
- Knowledge extraction reads stored artefacts and cannot become a source writer.
- Review is read-only until a human action is explicitly submitted; field/project review uses the
  existing review contract, not a second persistence state.
- The only executable engineering calculation remains the explicit NPT roll-up. Mud values are not
  converted or recalculated in promotion, indexing, review, search or staleness.
- Approval/status fields are not confirmation: admitted rows start as `CANDIDATE`. `CONFIRMED` requires
  an actor through the existing confirmation lifecycle.

## Machine inspection

```python
from drilling_intelligence.operations.contracts import contract_registry

assert len(contract_registry()) == 26
for contract in contract_registry():
    print(contract.to_dict())
```

The import-time registry guard fails if a new enum member is added without an explicit contract and
matrix entry. This file is the human-readable forensic index; the registry and executable tests remain
the machine authority.
