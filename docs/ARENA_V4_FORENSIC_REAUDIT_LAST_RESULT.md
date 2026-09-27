# Arena V4 forensic re-audit — last result

**Audit date:** 2026-09-23 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0c936-prog-proc`
**Audited HEAD (the V4 work as it was committed):** `f6a997613812d67d54386b6434ce3d8e7bdc1c7a`
**HEAD after this audit's fixes:** `ce7f24f75db5ff9818fad37b8d97bde31bd60cb4`
**Baseline:** `b7703baf35abd84881432a93264a979c33751c6c` (not an ancestor of HEAD; see §1)
**Authority:** the checked-out repository. No prior Arena report, commit message, or documentation
claim was accepted as evidence.

This is a re-audit, not a re-certification. It re-derived every V4 claim from source, ran the real
ingestion path, and attacked the shared helper the four source-shaped contracts all read through.

**Outcome: 4 code defects, 7 documentation defects and 2 test-coverage gaps were found. All are
fixed, and every fix is covered by a test that was executed in this audit.** One of the four code
defects was introduced by fixing another, which is recorded plainly in §6 rather than smoothed over.

---

## 1 — Baseline forensics

`b7703baf` was **not present** in this shallow clone. `git rev-parse --verify` echoed it back only
because the string is well-formed hex; `git cat-file -t b7703baf…` failed. It was recovered with
`git fetch --depth=50 origin b7703baf35abd84881432a93264a979c33751c6c`.

The baseline is **not an ancestor of HEAD** — the two are divergent lines. `git diff b7703baf..e862113`
touches only 3 documentation files, so `b7703baf..HEAD` is, to within those documents, the V4 diff:

| Range | Files | Insertions | Deletions |
| --- | ---: | ---: | ---: |
| `b7703baf..HEAD` | 38 | +7868 | −335 |
| `e862113..HEAD` (V4 only) | 35 | +7772 | −325 |

`git diff --check` reported no whitespace or conflict-marker errors.

## 2 — V3 regression

`tests/integration/test_v3_forensic_corpus.py` was deleted by V4. That deletion is **justified**: all
12 of its classification expectations are carried forward verbatim inside
`tests/integration/test_v4_forensic_corpus.py`, which adds 2 cases (14 total). A programmatic diff of
the deleted file's expectation map against the new one found 12 preserved, 0 changed, 2 added. No
assertion was weakened and none was lost.

**Defect found and fixed:** the documentation still cited the deleted code — see §5.

## 3–5 — BHA, bit record and directional survey verticals

Each vertical is complete on all the layers that make a feature real: parser with an explicit narrow
contract, model plus migration, promotion writer, repository/service read path, search and retrieval
exposure, review exposure, integrity checks, CLI, and real-path tests.

Scope safety is enforced and negatively tested in all three:

| Diagnostic | Meaning | Real-path test |
| --- | --- | --- |
| `WELL_SCOPE_CONFLICT` | the source names a different well | `test_bha_promotion_v4.py:220`, `test_bit_record_promotion_v4.py:185`, `test_directional_survey_promotion_v4.py:238` |
| `AMBIGUOUS_BHA_LINK` | two current assemblies share the bit's BHA number; link left NULL | `test_bit_record_promotion_v4.py:181` |
| `AMBIGUOUS_STATIONS` | station numbers do not identify a station uniquely | `test_directional_survey_promotion_v4.py:196` |
| `AMBIGUOUS_SECTIONS` | explicit section attributes match more than one well section | `test_bha_promotion_v4.py:276`, `test_program_promotion.py:220`, `test_promotion_contracts.py:141` |
| `SECTION_NOT_FOUND` | the named section is not a durable section of the well | `test_bha_promotion_v4.py:286` |
| `NO_RECOGNISED_TABLE` | the classification is admitted but the shape is not | 5 test references |

`promote.py` raises `AMBIGUOUS_SECTIONS` from two distinct places, and only one of them was covered.
Line 1132 is the shared `_explicit_section` refusal, tested by the three references above. Line 2653
is a different refusal in the survey sweep, fired when a *single survey set* names more than one
section — the source disagreeing with itself rather than failing to match the well. That path had no
test at all.

**Coverage gap found and closed:** `test_one_survey_set_naming_two_sections_attaches_neither` now
covers line 2653, asserting the diagnostic, that both stations still survive, that `section_id` is
NULL and that `section_resolution == "NOT_STATED"`. Verified by mutation: replacing the
`len(section_names) > 1` condition with `False` makes the test fail, so it is asserting the real code
path rather than passing incidentally.

No engineering value the source did not state is produced anywhere: no footage, ROP, bit wear, dull
grade, TVD, northing, easting, dogleg severity, trajectory or interpolation. Supplied TVD/N/E/DLS are
preserved verbatim with their source units. No unit is converted.

The BHA-to-bit link is made only on an exact, unambiguous, same-well, currently-current BHA number
(`promote.py:_linked_bha`); every other case leaves the link NULL and says so. In the 14-file corpus
run this yields `linked_to_bha == 1` of 2 bit runs, because the source names BHA "13" which does not
exist.

## 6 — `operations/tableshape.py` — the highest-leverage module

`tableshape.py` is the one module that knows how a stored table is walked; `mud.py`, `bha.py`,
`bit_record.py` and `survey.py` all read through it. A wrong answer there is the same wrong answer in
four domains at once, and it is invisible downstream because the value looks ordinary by the time it
reaches a row. Four defects were found here and in its one caller that misused it.

**Real defect 1 — `header_unit` alternation precedence.**
`re.search(r"\(([^)]+)\)|\b(units)\b", text)` matched whichever alternative came first, so
`"Depth In (ft)"` returned `"In"`. Now parenthesised-only. A bare-token fallback must not be
re-added.

**Real defect 2 — `header_unit` accepted any bracketed text as a unit.**
`"Remarks (optional)"` returned `"optional"`, and `"Date (as reported by driller)"` returned 22
characters against `String(16)` unit columns. Because promotion sets
`quality = "VALID" if source_unit else "UNVERIFIED"`, this marked values *verified* with garbage
units. Fixed: the parenthetical is accepted only if `normalise_label(candidate)` is in the caller's
`units` vocabulary. All real units still resolve (`in`, `ft`, `m`, `ppg`, `cP`, `pct`, `mg/l`, `deg`,
`deg/100ft`); annotations are refused.

**Real defect 3 — `numeric` stripped every comma.**
`"12,5"` returned `125.0` — a silent tenfold error. Commas are now accepted only when they match
`[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?`, so `"1,234"` → `1234.0` while `"12,5"`, `"1,2,3"` and
`"1234,567"` → `None`.

**Real defect 4 — mud reused its in-cell unit vocabulary for header units.**
`mud.py` defines its own `header_unit` wrapper passing `_MUD_VALUE_UNITS`, whose docstring states it
is the vocabulary for a unit *behind a number in a cell* (`"10.2 ppg"`). It contains no `ft`, `m`,
`in` or `h`. Before defect 2 was fixed this was harmless, because any bracketed text was accepted;
fixing defect 2 therefore silently began dropping the unit of every `Depth (ft)`, `Depth (m)`,
`Hole Size (in)` and `Time (h)` column — downgrading real measurements to `UNVERIFIED`. Verified by
direct probe:

| Header | mud, before this fix | mud, after |
| --- | --- | --- |
| `Mud Weight (ppg)` | `ppg` | `ppg` |
| `Depth MD (ft)` | `''` | `ft` |
| `Hole Size (in)` | `''` | `in` |
| `Time (h)` | `''` | `h` |
| `Remarks (optional)` | `''` | `''` |

Fixed by introducing `_MUD_HEADER_UNITS`, the closed union of the existing `_MUD_LABEL_UNITS` and
`DEFAULT_UNIT_TOKENS` — a wider *explicit list*, not an open rule. `_MUD_VALUE_UNITS` still governs
in-cell units unchanged. The generated corpus does not exercise those headers, which is exactly why
only the new tests stand between the bug and a workspace.

The BHA, bit and survey vocabularies were checked for the same class of error and are correct: each is
scoped to its own measurement kinds (BHA length/diameter; bit size/depth/hours; survey angles and
depths) and complete for them.

`tests/unit/test_tableshape.py` was added: **85 tests**, all passing.

## 7 — Mud conflict handling

Verified by the 7 executed tests in `test_mud_duplicate_properties_v3.py`. Summary rows are grouped by
property name. More than one copy with equal values keeps the first and reports `DUPLICATE_PROPERTY`.
More than one copy with differing values writes every copy as `quality="CONFLICT"`,
`is_current=False`, `status=CANDIDATE`, with `attributes["conflict"]` and a `mud_measurement`
conflict count; `result.outcome == "CONFLICT"`. Skip *reason* is `DUPLICATE_PROPERTY` in both cases —
only `detail` distinguishes them (it contains `"disagree"`). No `REVIEW_REQUIRED` status was
introduced: `ConfirmationStatus` remains closed at `CANDIDATE`/`CONFIRMED`/`REJECTED`.

## 8 — Models and migration 0011

`METADATA_REVISION == "0011"`; the chain is `…0009 → 0010 mud_report_domain → 0011
bha_bit_survey_domains` (head). Verified by executing a real upgrade on a scratch database:

```
alembic -c alembic.ini -x "url=sqlite:////tmp/reaudit.db" upgrade head   → exit 0
Running upgrade 0010 -> 0011, add the V4 BHA, bit-run and directional-survey domains
tables: 45    alembic_version: [('0011',)]
v4 tables present: ['bha_component','bha_report','bit_record','survey_run','survey_station']
```

Each of the five tables carries `UniqueConstraint("identity_key", name="uq_<table>_identity")`, and
the migration's index names and order match the models exactly.

## 9–12 — `promote.py`, identity, idempotence, history

Identity is `promote:` + `sha256(version_id, kind, table_id, row_index, well_id, extra)[:32]`. V4
writers always pass `row_index=0` and carry the real semantics in `extra`:

| Row | `extra` |
| --- | --- |
| BHA report | `bha_number` |
| BHA component | `f"{bha_number}\|{sequence}\|{normalise_label(source_label)}"` |
| Bit record | `f"{bit_number}\|{run_number}"` |
| Survey run | `run_label` or `""`, with `attributes["run_identity"]` = `LABEL` / `TABLE` |
| Survey station | `f"{run_label}\|{station_key}"`; `NUMBERED` when all numbers are present and unique, else `f"row:{sequence}"` plus `AMBIGUOUS_STATIONS` |

Survey sets are grouped by `(table_id, run_label)` and never merged. Station keys cannot collide
across groups within one table because `sequence` is assigned per table, and identity includes
`run_label`.

Re-promotion was verified end to end. Re-ingesting creates a new version; the earlier parent and its
children become `SUPERSEDED` with their values intact and readable, while the new rows are current.
Restating bit 12's footage gives a current value of 5700.0 with the superseded 5600.0 still readable.
`counts["removed"]` is not part of the contract. `_confirm_row` never overwrites a row a person may
have confirmed.

## 13–16 — Read surfaces, review, integrity, CLI

`record_summary` exposes `bha_reports`/`bit_records`/`survey_runs` at top level behind a
`v4_available` gate, plus nested `bha`, `bit`, `survey` and `mud` blocks. Every figure is a
database-side `COUNT`/`SUM`: the number of queries is fixed by the number of domains, not by the
number of rows. `check_domain_identities` counts children with one grouped query for the whole domain
rather than one query per parent. **No N+1 was found.**

Retrieval discovers candidates only through the disposable search index, so promotion without
`SearchService.for_workspace(ws).rebuild()` correctly yields an empty bundle. Review exposes V4
parents only — there is no `bha_component` record type; components are folded into the parent's
`data_extra`, and `ReviewRecord.provenance` is deduplicated to one entry per distinct locator.

**Coverage gap found and closed.** `doctor` had a test for a row the index had not seen, but it drove
a *lesson*. Nothing proved the five new V4 tables reach the structured-index counters. Executed
against a freshly promoted, never-indexed V4 corpus, `doctor --json` returns exit 1 with:

```
structured_missing: 18
findings:
  5 unresolved knowledge conflict(s): `drillintel knowledge conflicts`
  the search index disagrees with the registry: `drillintel index rebuild`
  18 structured row(s) not yet indexed, 0 no longer searchable, 0 orphaned: `drillintel index rebuild`
```

The 18 decompose as 5 `npt_record`, 3 `problem_occurrence`, 3 `well_event`, 2 `problem_definition`,
2 `bit_record`, and 1 each of `bha_report`, `survey_run`, `mud_report` — read from
`structured_records(session)`, not estimated. `test_doctor_names_the_v4_rows_the_index_has_not_seen`
now asserts that exact count, that the finding fires, and that `index rebuild` clears it while the
knowledge-conflict finding correctly remains.

The orphan-child check uses `notin_(select(parent.id))`; because SQL `NULL NOT IN (…)` is NULL rather
than TRUE, an unlinked bit record is not a false orphan. This is asserted on the real path by
`test_a_promoted_corpus_passes_the_domain_invariant_checks` (`tests/integration/test_v4_read_surfaces.py:291`),
which expects both `check_domain_identities(session) == []` and
`check_operational_integrity(session) == []` on a workspace where only 1 of 2 bit runs is linked.

CLI verified by execution: `records list --workspace … --table bha|bit|survey --well A-3` exits 0 and
prints `1 bha row(s)` / `2 bit row(s)` / `1 survey row(s)`. `--table` choices are argparse-enforced.
`doctor --workspace …` exits 1 on a freshly promoted, never-indexed V4 workspace with the correct
findings ("the search index disagrees with the registry", "18 structured row(s) not yet indexed").

## 17 — Test quality

The V4 suites are real-path: they drive `IngestionPipeline` and
`OperationalService.promote_workspace` over generated source files and assert on persisted rows, not
on parser return values. Every safety diagnostic in §3 has at least one executed negative test.

**Coverage gap found:** the mud fixture's daily headers are `MW in (ppg)`, `MW out (ppg)`,
`Visc (cP)`, `Sand (pct)` — all inside `_MUD_VALUE_UNITS`. Nothing in the corpus exercised a `ft`,
`m`, `in` or `h` mud header, which is why defect 4 was invisible to the suite. The three new tests in
`test_tableshape.py` close that gap.

## 19 — Architectural cleanup

Evidence-based only; nothing was deleted. `_list_source_versioned` is the single listing shape shared
by the four source-versioned domains, which is what stops them drifting into four different ideas of
what a field-scoped filter means. `DOMAIN_SWEEP_ORDER` remains deleted. The three V4 sweep blocks
(BHA ~2106, bit ~2394, survey ~2785) are structurally parallel, including the
parent-after-children orphan deletion order that keeps the `report_id` foreign key satisfiable.

## 20 — Documentation consistency

**Defects found and fixed.** `docs/DOCUMENT_DOMAIN_COVERAGE.md` and
`docs/PRODUCTION_INGESTION_V3_CERTIFICATION.md` still described the repository as it was at V3:

| Claim in the docs | Actual, read from `contract_registry()` |
| --- | --- |
| domain handlers **5** | **6** — `program`, `report`, `mud_report`, `bha_report`, `bit_record`, `directional_survey` |
| `END_TO_END_CERTIFIED` **5** | **8** — adds `BHA_REPORT`, `BIT_RECORD`, `DIRECTIONAL_SURVEY` |
| `KNOWLEDGE_SUPPORTED` **16** | **13** |
| `BHA_REPORT` / `BIT_RECORD` / `DIRECTIONAL_SURVEY` → "no handler" | each has a named handler and target models |
| static contract list omitted the three V4 contracts | `…:promotion:v4` for all three |
| `build_v3_forensic_corpus()` / `test_v3_forensic_corpus.py` | both deleted in V4 |
| "did not increase support coverage for the six secondary classes" | false for BHA, bit and directional survey |

`DOCUMENT_DOMAIN_COVERAGE.md` was corrected to the numbers above and re-headed as the V4 authority.
`PRODUCTION_INGESTION_V3_CERTIFICATION.md` is a dated certification record, so its V3 findings were
left intact and a supersession banner was added naming the three classes V4 promoted; the stale
corpus pointers now state that both files were removed and where the twelve expectations went. The
remaining mentions of the deleted names are that intentional historical note.

`DocumentClassification` still has 26 members, 26 contracts, 0 `DOMAIN_PROMOTABLE`-but-uncertified,
and 5 `EXTRACT_ONLY`; 8 + 13 + 5 = 26. Contract revisions: 22 `v2`, 1 `v3`, 3 `v4`. No existing
contract ID was silently changed.

## 21 — Validation actually executed

Every check below was executed against the working tree as it now stands, with all fixes in place.

| Check | Command | Result |
| --- | --- | --- |
| Full suite | `python -m pytest -p no:cacheprovider --tb=short` | **exit 0 — 1288 passed, 3 skipped, 0 failed** |
| Collection | `pytest --collect-only -q` | **1291 tests collected** |
| Shared-helper suite | `pytest tests/unit/test_tableshape.py` | **85 passed** |
| Survey vertical | `pytest tests/integration/test_directional_survey_promotion_v4.py` | **17 passed** |
| Read surfaces | `pytest tests/integration/test_v4_read_surfaces.py` | **21 passed** |
| Migration on a scratch DB | `alembic -c alembic.ini -x "url=sqlite:////tmp/reaudit.db" upgrade head` | exit 0, 45 tables, `version_num = 0011` |
| Byte-compile | `python -m compileall src tests migrations` | exit 0 |
| Lint | `ruff check src/ tests/` | **All checks passed!** |
| Whitespace/conflict markers | `git diff --check` | clean |

The `pyproject.toml` `addopts = "-q --strict-markers"` combined with a second `-q` on the command line
suppresses pytest's summary line, so the pass count was taken from the collected total and the result
characters rather than assumed: 1291 collected = 1288 `.` + 3 `s`. That also cross-checks against the
pre-audit suite, which collected 1204; 1204 + 85 (`test_tableshape.py`) + 2 (the survey two-section and
doctor tests added here) = 1291 exactly.

The three skips were identified rather than assumed: all three are `tests/ui/test_workbench.py` (lines 56, 120, 184) skipping on `QtWidgets unavailable for UI tests: No module named 'PySide6'`. They are environmental, pre-existing, and unrelated to V4.

Mutation-checked, so the new tests are known to assert the real code path: suppressing the survey
`AMBIGUOUS_SECTIONS` condition at `promote.py:2653` makes
`test_one_survey_set_naming_two_sections_attaches_neither` fail.

## 22 — Remaining limitations

These are stated, not hidden:

1. **Bit-to-BHA linking is name-based only.** A source that does not state a BHA number, or states one
   the well does not have, yields a NULL link with a diagnostic. There is no inference from dates,
   depths or run order.
2. **Two unlabelled survey sets in one table remain one run.** Grouping is `(table_id, run_label)`;
   where the source states no run label, the table is the set. This is documented in the source
   comment as a contract limit rather than resolved by a silent merge.
3. **A wholly unnumbered station set produces no `AMBIGUOUS_STATIONS` diagnostic.** `station_identity`
   is `UNNUMBERED` and identity falls back to `row:{sequence}`, which is deterministic. The diagnostic
   fires only for *conflicting* numbering. Defensible, but it means an unnumbered set is reported less
   loudly than a badly numbered one.
4. **`DECISIONS.md` has no ADR for V3 or V4.** This matches the repository's own convention — those
   contracts are certified in `docs/*_CERTIFICATION.md`, and the highest ADR is 0024. Not a
   contradiction, but the ADR block is also out of numeric order (0020 appears after 0024), which
   predates V4 and was left alone rather than rewritten.
5. **`bha_component` and `survey_station` are not individually searchable.**
   `STRUCTURED_RECORD_TYPES` contains 10 entries and includes `bha_report`, `bit_record` and
   `survey_run` but neither child table. Children are reached through their parent, which matches the
   rule review already follows, so it is consistent rather than accidental — but a query for "every
   component with a 6 1/2 in OD" cannot be answered from the index, only from the repository listing.
6. **`CONFIRMABLE_MODELS` was not extended for V4.** It has 12 entries and none of the five V4
   tables. V4 rows start `CANDIDATE` and are confirmed through the existing lifecycle.
7. **The mud fixture does not exercise `ft`/`m`/`in`/`h` headers.** The new unit tests do. A corpus
   case would be stronger still.

## Certification

**`CERTIFIABLE_WITH_DOCUMENTED_LIMITATIONS`**

The V4 surface is real end to end and the four defects found in this re-audit are fixed and covered by
executed tests. The limitations above are narrow, deterministic and stated; none of them fabricates a
value, converts a unit, or attaches data to a scope the source did not name.
