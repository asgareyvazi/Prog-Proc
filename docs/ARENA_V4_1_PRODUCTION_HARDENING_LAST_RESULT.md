# Arena V4.1 production hardening — last result

**Date:** 2026-09-23 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0c936-prog-proc`
**HEAD before this mission:** `292830e127a4b0902ba217671d37bdfaa1f9b4ab`
**HEAD after this mission:** `f4b45fb565ee5c553ebc92e3177355ed6c9e9ed9` (pushed; verified by both
`git ls-remote` and `gh api`)
**V4 implementation audited:** `f6a997613812d67d54386b6434ce3d8e7bdc1c7a`
**V3 baseline:** `b7703baf35abd84881432a93264a979c33751c6c` (not an ancestor of HEAD)
**Authority:** the checked-out repository. Every claim below comes from a command run during this
mission; the previous audit report was treated as a set of claims to re-derive, not as evidence.

---

## 1 — Structured index boundary

**Determined: the index is model B — top-level records only — and that is architecturally correct.**

`structured_missing` is *not* a measure of a semantic gap. It is computed at
`search/index.py:913` and `:1501` as:

```python
stats.structured_missing = len(searchable - stored)
```

— a set difference between the rows the domain considers searchable and the rows the disposable index
currently holds. Promotion deliberately does not build the index; `index rebuild` does. So a freshly
promoted, never-indexed workspace reports every searchable row as missing, which is the designed
behaviour, not a defect.

The boundary is **not** "parents only". Verified from the schema:

| Indexed type | Has a parent foreign key? |
| --- | --- |
| `npt_record` | yes — `report_id → ddr_report`, plus operation/event/section/rig |
| `well_event` | yes — `report_id → ddr_report`, plus operation/section |
| `problem_occurrence` | yes — `problem_definition_id`, `operation_id`, `event_id`, `npt_id` |
| `bit_record` | yes — `bha_report_id → bha_report` |

A parent foreign key is therefore *not* what excludes a table. What excludes a table is being a
**measurement inside a parent's own statement** rather than an answer to a question about a well on
its own.

## 2 — Child-record policy

`bha_component`, `survey_station` and `mud_measurement` are outside `STRUCTURED_RECORD_TYPES`
**deliberately**, and what makes that safe is that each parent builder folds its children in — both
values and provenance. From `search/structured.py`:

* `_bha_report` walks `scope.bha_components(row.id)`, emits `component {sequence}: {source_label}
  {type} {manufacturer} {model} {serial} {OD/ID/length with units} {qty}` into the searchable text,
  and collects each component's own provenance into `component_evidence`.
* `_survey_run` does the same via `scope.survey_stations(...)` → `station_evidence`.
* `_mud_report` does the same via `scope.mud_measurements(...)` → `measurement_evidence`.

`_Scope` preloads all three child tables in **one query each** (3 queries total per rebuild), keyed by
parent id — so folding is not an N+1.

Measured on the real corpus, the folded provenance counts exactly equal the persisted child rows:

| Parent | Child rows persisted | Child provenance entries folded into the parent unit |
| --- | ---: | ---: |
| `mud_report` (1) | 22 `mud_measurement` | 22 |
| `bha_report` (1) | 6 `bha_component` | 6 |
| `survey_run` (1) | 5 `survey_station` | 5 |

**No child provenance is lost.** Decision: the boundary is correct and is now codified rather than
left implicit. Child tables were **not** added to the index — doing so to quiet `doctor` would have
duplicated every child as its own searchable unit while the parent already carries it.

## 3 — Doctor semantics

`doctor` exits non-zero when findings exist. Two findings fire on a freshly promoted V4 corpus, and
they mean different things:

| Finding | Meaning | Correct? |
| --- | --- | --- |
| `N structured row(s) not yet indexed … index rebuild` | the disposable index is behind the registry | yes — promotion does not build the index |
| `the search index disagrees with the registry` | same cause, reported at registry level | yes |
| `5 unresolved knowledge conflict(s)` | five properties are disputed and no person has decided | yes — see §4 |

`doctor` was **not** made green. Both findings are true statements about the workspace.

## 4 — The five knowledge conflicts

Reproduced directly from the database (`select … from knowledge_conflict`), not read from a report:

| # | Property | Note | Distinct values | Sources |
| ---: | --- | --- | --- | --- |
| 1 | `hole_depth` | 6 different values stated by 5 sources | 9000, 9100, 9780, 9850, 9940, 10125 ft | bha_tally, DDR, NPT csv, mud report, lesson |
| 2 | `hole_section_size` | 2 different values stated by 3 sources | 8.5 in, 12.25 in | bha_tally, DDR, lesson |
| 3 | `mud_volume` | 2 different values stated by 3 sources | 12 bbl, 1450 bbl | kill sheet, mud report, DDR |
| 4 | `rpm` | 2 different values stated by 2 sources | 120 rpm, 300 rpm | DDR, mud report |
| 5 | `surface_pressure` | 3 different values stated by 2 sources | 420 psi (SIDPP), 610 psi (SICP), 1850 psi | kill sheet, DDR |

All five are `status = OPEN`, `resolution = NULL`, `detected_by = knowledge.conflicts`. Every
candidate retains `document_id`, `document_version_id`, `filename`, `parser`, `excerpt`,
`source_sha256` and a locator. **No winner is picked** — `detect_conflicts` marks *every* voice, not
just the minority one, because flagging only the odd value out would imply the majority is the answer.

Knowledge items: 83 total — 50 `ACTIVE`, 13 `UNVERIFIED`, 20 `CONFLICTED`.

**Are they legitimate?** Yes, and they are irreducible by machine:

* `hole_depth` — the corpus genuinely states different depths at different moments (a lesson about a
  9,000 ft incident, a DDR at 10,125 ft, an NPT event at 9,940 ft). Settling it is a human decision.
* `hole_section_size` — two genuinely different hole sections.

**One real limitation found:** three of the five (`surface_pressure`, `mud_volume`, `rpm`) arise
because knowledge extraction maps *distinct physical quantities* onto one predicate — SIDPP, SICP and
a surface-pressure **limit** all become `surface_pressure`; a kill-sheet **pill volume** and a
**total system volume** both become `mud_volume`; a rheometer's **300 rpm** and a rotary's **120 rpm**
both become `rpm`. This is safe — nothing is merged, no winner is chosen, both sides stay retrievable
— but it is imprecise, and it inflates the conflict count with comparisons an engineer would not
consider the same quantity. It belongs to the knowledge layer, not the V4 operational domains, and
fixing it means narrowing predicates, which is a knowledge-extraction change outside this mission's
scope. Recorded as a limitation rather than papered over.

`detect_conflicts` already distinguishes three cases, and this is now asserted:

* **conflict** — ≥2 *sources* state different values → `OPEN`, counted by `doctor`;
* **agreement** — ≥2 sources state the *same* value → corroboration, `ConflictReport.agreements`,
  **not** a conflict;
* **ambiguous** — two values inside *one* revision of *one* file → `ConflictReport.ambiguous`; the
  knowledge layer does not claim a document contradicts itself.

## 5 — Whether child records are indexed

No. See §2. Asserted deliberately by
`tests/unit/test_structured_index_boundary.py::test_child_tables_are_deliberately_outside_the_index_vocabulary`,
so the exclusion cannot become accidental in either direction.

## 6 — Retrieval behaviour

Executed against a rebuilt index over the real corpus (`SearchService.for_workspace(ws).rebuild()`):

| Query | Result |
| --- | --- |
| `bottom hole assembly` | `bha_report` |
| `component 4` | `bha_report` — the child fact is found through its parent |
| `bit 12` | `bit_record` |
| `pull reason` | 2 × `bit_record` |
| `dull grade` | 2 × `bit_record` |
| `survey run` / `station 3` / `inclination` / `azimuth` | `survey_run` — station-level facts found |
| `mud weight` | `mud_report` |
| `record_types=["bha_report"\|"bit_record"\|"survey_run"\|"mud_report"]` | 1 / 2 / 1 / 1 hits |

Three queries returned nothing and each was traced to a cause rather than assumed:

| Query | Why | Verdict |
| --- | --- | --- |
| `chlorides` | the text holds `chloride_mg_l`; the tokenizer already splits underscores (`chloride_mg_l` → `chloride`), but there is no stemming, so the plural misses | exact-token matching by design |
| `non productive time` | the NPT unit text is `code: NPT` / `description: NPT - equipment`; the source never says the long form | source wording preserved, not expanded |
| `dogleg` | the station line emits the abbreviation `DLS`, which is what the source column was called | source token preserved |

In every case the underlying fact **is** indexed and reachable (`chloride`, `NPT`, `DLS` all hit).
No synonym expansion was added: inventing labels the source did not use to make a query succeed is
exactly the kind of heuristic this system exists to avoid.

## 7 — Unit policy

Verified end to end through real ingestion and promotion in
`tests/integration/test_mud_unit_matrix_v4.py` (9 tests).

| Header | Stored unit | Quality |
| --- | --- | --- |
| `Depth MD (ft)`, `TVD (m)` | `ft`, `m` | VALID |
| `Mud weight (ppg)`, `PV (cP)`, `YP (lb/100ft2)` | verbatim | VALID |
| `Chloride (mg/l)`, `Total mud volume (bbl)`, `Pore pressure gradient (psi/ft)` | verbatim | VALID |
| `Mud weight (sg)`, `YP (Pa)`, `PV (Pa s)` | `""` | UNVERIFIED |
| `Hole size (mm)`, `Total mud volume (m3)`, `Pore pressure gradient (kPa/m)` | `""` | UNVERIFIED |
| `Remarks (optional)`, `Date (as reported by driller)` | `""` | refused as an annotation |

Policy, all asserted:

* **No conversion.** A depth filed in metres is stored as metres; `normalized_value` and
  `normalized_unit` stay NULL on every row.
* **No guessing.** A unit outside the closed vocabulary is refused. Verified that `sg`, `mm`, `m3`,
  `Pa`, `kPa/m` and `Pa s` never appear as a stored mud unit.
* **The refusal is lossless.** `promote.py:1672` and `:1686` pass `source_label=entry.header`, so a
  refused measurement still carries the operator's own header text — asserted that
  `"MW out (sg)" in refused.source_label`. `source_value_text` also survives on every row.
* **A refused unit degrades one row, never the document.** The metric report still promotes with
  `outcome == "PROMOTED"`.

Two facts established rather than assumed:

* The mud contract **requires** both a summary block and a repeated daily table; a workbook with only
  one is refused with `MISSING_PROVENANCE` (`_missing_promotion_evidence`).
* `MUD_SUMMARY_METADATA` (`well`, `field`, `report_date`, `revision`, `section_id`, `section`,
  `hole_size_in`) is read into the parent row and **never** written as a measurement. `hole_size_in`
  is still unit-parsed, because it is what matches a hole section — asserted at parser level that
  `Hole size (in)` → `in` while `Hole size (mm)` → `""` with its value intact.
* `h`/`hr`/`min` do not arise in this contract: a mud report's `Time` column is the sample clock
  (`06:00`), stored as text and never unit-parsed. Those tokens are exercised against the shared
  helper in `tests/unit/test_tableshape.py` instead.

## 8 — Identity policy

`promote:` + `sha256(version_id, kind, table_id, row_index, well_id, extra)[:32]`. V4 writers pass
`row_index=0` and carry the real semantics in `extra` (BHA number; `bha|sequence|label`;
`bit|run`; run label or `""`; `run_label|station_key`).

Verified at the database level on a freshly migrated schema — the named unique constraint exists on
all five tables and is enforced:

```
bha_report       CONSTRAINT uq_bha_report_identity       UNIQUE (identity_key)
bha_component    CONSTRAINT uq_bha_component_identity    UNIQUE (identity_key)
bit_record       CONSTRAINT uq_bit_record_identity       UNIQUE (identity_key)
survey_run       CONSTRAINT uq_survey_run_identity       UNIQUE (identity_key)
survey_station   CONSTRAINT uq_survey_station_identity   UNIQUE (identity_key)

insert duplicate identity_key → IntegrityError: UNIQUE constraint failed: survey_station.identity_key
```

Note the precise reading: SQLite reports the backing index as `sqlite_autoindex_<table>_2` because it
always auto-names the implicit index behind a UNIQUE constraint. The *constraint* is named as above
in the stored DDL, and model and migration declare it identically.

## 9 — History policy

`is_searchable` applies one rule to all four source-versioned parents: a row a newer version of the
same document replaced is history, not the answer to "what is in the hole now" — so superseded and
rejected rows are not projected, exactly as a superseded document version is not. Values are never
destroyed; the earlier row remains readable beside the current one.

## 10 — Migration safety

| Path | Command | Result |
| --- | --- | --- |
| Clean database | `alembic … upgrade head` | exit 0, 45 tables, `version_num = 0011` |
| V3 schema, then upgrade | `upgrade 0010` → 40 tables, no V4 tables, `0010` | then `upgrade head` → 45 tables, all 5 V4 tables, `0011` |
| Downgrade | `downgrade()` present | drops the 5 V4 tables and their indexes in child-first order |

Purely additive: no V3 table is dropped, altered or rewritten by 0011.

## 11 — Performance

No N+1 found in the paths examined:

* `_Scope` — 3 preloaded queries for all children of all parents.
* `record_summary` — every figure is a database-side `COUNT`/`SUM`; query count is fixed by the number
  of domains, not the number of rows.
* `check_domain_identities` — one grouped child count per domain, not one per parent.
* `_list_source_versioned` — one query per listing, shared by all four source-versioned domains.

## 12 — Defects found and fixed

| # | Defect | Evidence | Fix |
| --- | --- | --- | --- |
| 1 | `search/structured.py` module docstring no longer described the design: it said the projection reads **"the seven admitted model tables"** (there are 10), listed only "problems, NPT, events, lessons, recommendations and mud reports" (omitting BHA/bit/survey), and never stated the child-folding rule that makes the whole boundary safe | `_RECORD_SOURCES` has 10 entries; `STRUCTURED_RECORD_TYPES` has 10 | docstring rewritten to state the boundary, the child policy, and the fold |
| 2 | Nothing asserted that `STRUCTURED_RECORD_TYPES`, `_RECORD_SOURCES` and `_BUILDERS` describe one set. A type added to the vocabulary without a source would never be projected **and** never counted as missing, because `structured_searchable_ids` walks `_RECORD_SOURCES` — the index would report clean over an unsearchable domain | all three agreed only by coincidence | `tests/unit/test_structured_index_boundary.py` (7 tests) |
| 3 | No test drove the mud unit path outside the imperial vocabulary, so behaviour on `sg`/`mm`/`m3`/`Pa`/`kPa` was unknown rather than decided | fixture headers are ppg/cP/pct only | `tests/integration/test_mud_unit_matrix_v4.py` (9 tests) |
| 4 | The five knowledge conflicts were reported as a number and never characterised; the agreement/conflict/ambiguity distinction was untested | `ConflictReport.agreements` / `.ambiguous` existed with no test | `tests/integration/test_knowledge_conflict_semantics_v4.py` (6 tests) |

Defect 2's guard was **mutation-checked**: adding `bha_component` to `STRUCTURED_RECORD_TYPES` with no
source or builder makes 2 of the 7 tests fail. Reverted; the vocabulary is back to 10 and the suite
passes.

## 13 — Remaining limitations

1. **Three of the five knowledge conflicts conflate distinct physical quantities** under one
   predicate (`surface_pressure`, `mud_volume`, `rpm`). Safe but imprecise; belongs to knowledge
   extraction. See §4.
2. **Metric units are refused, not understood.** `sg`, `mm`, `m3`, `Pa`, `kPa/m` leave a measurement
   `UNVERIFIED` with no unit. The refusal is lossless (the header text survives) but a metric operator
   will see more `UNVERIFIED` rows than an imperial one. Adding them is a one-line vocabulary change
   now covered by a test.
3. **Search matches exact tokens.** No stemming or synonym expansion, so `chlorides`, `dogleg` and
   `non productive time` miss where `chloride`, `DLS` and `NPT` hit. Deliberate.
4. **Children are not independently searchable.** Reachable through the parent's unit and its
   `*_evidence`, and through the repository listings — but a filter for "every component with a
   6½ in OD" is a repository query, not an index query.
5. **`doctor` exits 1 on this corpus and should.** It is not a defect and must not be "fixed".
6. **`bha_component` / `survey_station` have no `bha_component` record type in review.** Children are
   folded into the parent's `data_extra`, and `ReviewRecord.provenance` is deduplicated to one entry
   per distinct locator.
7. **`CONFIRMABLE_MODELS` (12 entries) was not extended for V4.** V4 rows start `CANDIDATE` and are
   confirmed through the existing lifecycle.

## 14 — Exact test results

Executed after all changes.

| Check | Command | Result |
| --- | --- | --- |
| New: unit matrix | `pytest tests/integration/test_mud_unit_matrix_v4.py` | **9 passed** |
| New: conflict semantics | `pytest tests/integration/test_knowledge_conflict_semantics_v4.py` | **6 passed** |
| New: index boundary | `pytest tests/unit/test_structured_index_boundary.py` | **7 passed** |
| Migration (clean) | `alembic … upgrade head` | exit 0, 45 tables, `0011` |
| Migration (from V3) | `upgrade 0010` → `upgrade head` | 40 → 45 tables, additive |
| Uniqueness | duplicate `identity_key` insert | `IntegrityError` — enforced |
| Lint | `ruff check src/ tests/` | **All checks passed!** |
| Byte-compile | `python -m compileall src tests migrations` | exit 0 |
| Full suite | `pytest -o addopts="--strict-markers"` | **see below** |

```
$ ./.venv/bin/python -m pytest -p no:cacheprovider -o addopts="--strict-markers" --tb=short
1310 passed, 3 skipped in 753.55s (0:12:33)
EXIT=0

$ ./.venv/bin/python -m pytest -o addopts="--strict-markers" --collect-only -q
1313 tests collected
```

The counts reconcile exactly: 1310 passed + 3 skipped = 1313 collected, and the baseline at HEAD
`292830e` collected 1291, so 1291 + 9 + 6 + 7 = 1313. The three skips are all
`tests/ui/test_workbench.py` on `QtWidgets unavailable for UI tests: No module named 'PySide6'` —
environmental, pre-existing and unrelated to these changes.

`pyproject.toml` sets `addopts = "-q"`, so the suite is run with `-o addopts="--strict-markers"`
rather than a second `-q`; a doubly-quiet invocation suppresses pytest's summary line and yields a
green exit code with no count to read.

## Certification

**`CERTIFIABLE_WITH_DOCUMENTED_LIMITATIONS`**

The structured-index boundary is correct, is now stated in the source and pinned by tests, and the
child fold preserves both values and provenance with counts that match the persisted child rows
exactly. The mud unit policy is decided, lossless and tested against a real metric report. The five
knowledge conflicts are legitimate, fully attributed, and correctly keep `doctor` red.

The limitations in §13 are narrow and stated. None fabricates a value, converts a unit, merges a
disputed value, or attaches data to a scope the source did not name. The two that most deserve
follow-up are the predicate conflation in knowledge extraction (§13.1) and the metric unit vocabulary
(§13.2).
