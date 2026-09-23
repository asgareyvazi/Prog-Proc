# Document/domain coverage matrix (V4 authority)

**Status:** authoritative source-derived registry and certification index
**As of:** 2026-09-23 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0c936-prog-proc`
**Inspected HEAD before final validation:** `f6a997613812d67d54386b6434ce3d8e7bdc1c7a`
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
| domain handlers | **6** | `program`, `report`, `mud_report`, `bha_report`, `bit_record`, `directional_survey` (DDR/NPT/TIME_BREAKDOWN share the named `report` handler) |
| `END_TO_END_CERTIFIED` | **8** | drilling program, DDR, NPT, mud report, time breakdown, BHA report, bit record, directional survey |
| `DOMAIN_PROMOTABLE` but not end-to-end certified | **0** | no remaining restricted domain writer |
| `KNOWLEDGE_SUPPORTED`, no domain writer | **13** | explicit deny-by-no-handler registry entries |
| `EXTRACT_ONLY`, no type-specific knowledge/domain contract | **5** | explicit deny-by-no-handler registry entries |
| deterministic V4 corpus cases | **14** | `build_v4_forensic_corpus()` and `test_v4_forensic_corpus.py` |

The six handler entries preserve the V2 contract IDs for existing writers. `MUD_REPORT` is revisioned
`v3`; `BHA_REPORT`, `BIT_RECORD` and `DIRECTIONAL_SURVEY` are revisioned `v4`. No existing contract ID
was silently changed: 22 contracts remain `v2`, 1 is `v3`, 3 are `v4`.

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

| # | Classification | Classify | Extract | Knowledge | Static level | Handler / target | Review/search | Disposition and source decision |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `DRILLING_PROGRAM` | yes | yes | yes | `END_TO_END_CERTIFIED` | `program` -> `drilling_program`, `program_target`, planned `well_section` | yes | Certified; planned values and actual section values stay distinct. |
| 2 | `DDR` | yes | yes | yes | `END_TO_END_CERTIFIED` | `report` -> `ddr_report`, `well_operation`, `well_event`, `npt_record`, `problem_occurrence` | yes | Certified; typed/table rows only, not narrative guessing. |
| 3 | `MUD_REPORT` | yes | yes | yes | `END_TO_END_CERTIFIED` | `mud_report` -> `mud_report`, `mud_measurement` | yes | **Admitted V3.** Summary and repeated daily values retain source units, sample identity and locators. |
| 4 | `BHA_REPORT` | yes | yes | yes | `END_TO_END_CERTIFIED` | `bha_report` -> `bha_report`, `bha_component` | yes | **Admitted V4.** A component *tally* is promoted; a prose BHA narrative carries the same classification but is refused by shape. No inferred component type beyond the closed alias set. |
| 5 | `BIT_RECORD` | yes | yes | yes | `END_TO_END_CERTIFIED` | `bit_record` -> `bit_record` | yes | **Admitted V4.** Source-stated bit runs only; a BHA link is made only on an exact, unambiguous same-well number, else left NULL and reported. No footage/ROP/wear calculation. |
| 6 | `DIRECTIONAL_SURVEY` | yes | yes | yes | `END_TO_END_CERTIFIED` | `directional_survey` -> `survey_run`, `survey_station` | yes | **Admitted V4.** Stations are stored as reported. TVD/northing/easting/DLS are preserved only when the source states them; no trajectory mathematics or interpolation. |
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
| 18 | `TIME_BREAKDOWN` | yes | yes | yes | `END_TO_END_CERTIFIED` | `report` -> `ddr_report`, `well_operation`, `npt_record` | yes | Certified on a standalone real CSV: explicit activity/duration rows, source duration text, row provenance, actual candidate state, NPT-code boundary and idempotence. See [`TIME_BREAKDOWN_CERTIFICATION.md`](TIME_BREAKDOWN_CERTIFICATION.md). |
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
DRILLING_PROGRAM   -> document:DRILLING_PROGRAM:promotion:v2   -> program
DDR                -> document:DDR:promotion:v2                -> report
NPT                -> document:NPT:promotion:v2                -> report
TIME_BREAKDOWN     -> document:TIME_BREAKDOWN:promotion:v2     -> report
MUD_REPORT         -> document:MUD_REPORT:promotion:v3         -> mud_report
BHA_REPORT         -> document:BHA_REPORT:promotion:v4         -> bha_report
BIT_RECORD         -> document:BIT_RECORD:promotion:v4         -> bit_record
DIRECTIONAL_SURVEY -> document:DIRECTIONAL_SURVEY:promotion:v4 -> directional_survey
```

The mud contract requires a stored extraction, a recognised summary label/value/unit shape, a repeated
sample-labelled daily table, preserved source units, row/table provenance, a linked well and explicit
section resolution. A missing or ambiguous section is represented by `section_id = NULL`; it is never
filled from MD/TVD, row order, filename, nearest depth or a UI selection.

The three V4 contracts add their own required-evidence gate on top of the same rule. `BHA_REPORT`
requires a component description column plus at least one sizing column; `BIT_RECORD` requires a
bit-number column plus at least one measurement column; `DIRECTIONAL_SURVEY` requires measured-depth,
inclination and azimuth columns. Each is a narrow, named contract rather than a general table reader:
a source that carries the classification but not the shape is refused with `NO_RECOGNISED_TABLE` and
written nowhere.

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
- **The structured index holds top-level records, not child measurements.** Ten record types are
  projected (`STRUCTURED_RECORD_TYPES`): `problem_definition`, `problem_occurrence`, `npt_record`,
  `well_event`, `lesson_learned`, `recommendation`, `mud_report`, `bha_report`, `bit_record`,
  `survey_run`. `bha_component`, `survey_station` and `mud_measurement` are deliberately not indexed
  as their own units: each is a *measurement inside* a parent's statement rather than an answer to a
  question about a well. They are not hidden - each parent builder folds its children's values into
  the searchable text and carries their own provenance as `component_evidence` /
  `station_evidence` / `measurement_evidence`, so "which component was at sequence 4?" and "what was
  the inclination at MD 9500?" are answerable from a hit and traceable to the child's own locator.
  Having a parent foreign key is *not* what excludes a table: `npt_record`, `well_event`,
  `problem_occurrence` and `bit_record` all have one and are all indexed. The boundary is pinned by
  `tests/unit/test_structured_index_boundary.py`.
- `doctor` reports `structured_missing` as the difference between the rows the domain considers
  searchable and the rows the disposable index holds. Because promotion does not build the index, a
  freshly promoted workspace legitimately reports every searchable row as missing until
  `index rebuild` runs; a non-zero `doctor` there is a true statement, not a defect.
- Knowledge extraction reads stored artefacts and cannot become a source writer.
- Review is read-only until a human action is explicitly submitted; field/project review uses the
  existing review contract, not a second persistence state.
- The only executable engineering calculation remains the explicit NPT roll-up. Mud, BHA, bit and
  survey values are not converted or recalculated in promotion, indexing, review, search or
  staleness: footage, ROP, bit wear, dull grade, TVD, northing, easting, dogleg severity and
  trajectory are never derived when the source did not state them.
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
