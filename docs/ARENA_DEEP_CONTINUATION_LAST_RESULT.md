# V7.2B Deep Continuation — last result

Verdict: **PARTIALLY-CERTIFIED** — well control is certified end to end; HSE is certified on every
surface except two gaps that are recorded, not hidden.

Branch `arena/01a0c936-prog-proc`, base `8ec5003`. Work uncommitted at the time of writing.

## Defects found and fixed (all found on the real ingest → promote path)

1. `delete_promoted()` covered 12 of 23 derived version-owned tables. Live `Base.metadata` shows 23
   tables with `document_version_id` + `origin`; the tuple omitted exactly `cost_item`, `casing_run`,
   `cement_job`, `well_control_event`, `hse_incident`. Now 17 models in FK-safe order. Proven by
   probe: after `delete_promoted`, **zero** derived rows remain for the version.
2. `CauseStatus.SOURCE_STATED` does not exist (real members: `KNOWN`/`INFERRED`/`UNKNOWN`/
   `CONFLICTED`), so both V7.2 writers raised `AttributeError` on their first row. Three sites now
   use `CauseStatus.KNOWN`. **This retroactively invalidates the `END_TO_END_CERTIFIED` labels
   shipped in `8ec5003`** — no certification test existed to catch it.
3. `Date / Time`, the standard spelling on a real well-control log, was absent from both alias
   contracts (`normalise_label` keeps the slash, so `date/time` never matched). Until fixed, **every**
   well-control and HSE row had `occurred_at = NULL` **and** `occurred_at_text = ''`.
4. When `_mud_date` could not parse, it returned empty wording and discarded the source's own
   date text. Both writers now keep the wording verbatim with `occurred_at` NULL.
5. HSE fixtures classified as `LESSON_LEARNED` / `OTHER`; `incident_reference` was always `None`
   because the `Incident Report No` header was not an alias.

## Verification actually executed

* `tests/integration/test_well_control_promotion_v72.py` — **22 passed**, real XLSX through real
  ingestion, extraction, classification, promotion, then read back from the database.
* `tests/unit/test_structured_index_boundary.py` — 7 passed; `search/structured.py` now carries 14
  types and `set(STRUCTURED_RECORD_TYPES) == set(_BUILDERS) == {r for _, r, _ in _RECORD_SOURCES}`.
* Search projection verified live: `SIDPP` → 5 `well_control_event` hits; `near miss`, `laydown`,
  `dropped object` → `hse_incident` hits. Site-only HSE is findable with `well_id = None` and a real
  `project_id`.
* `tests/integration/test_hse_promotion_v72.py` — **16 of 17 pass**; one failure, described below.

## Open, and not certified

* **HSE well-scope conflict is not implemented.** `_promote_hse` has no `WELL_SCOPE_CONFLICT` check
  and `HseIncidentEntry` has no `well_name` field, so a row naming a well that contradicts its
  document is not detected — it inherits the document's well. `_promote_well_control` does check.
  This is the one failing test, and it fails because the assertion was written for the required
  behaviour. It is labelled in the test with the exact assertion to flip when the gap closes.
* **`spill_volume` and `npt_hours` are parsed but not stored.** `hse_incident` has neither column.
  The safe half holds and is tested — `npt_id` is NULL on every row and no `NptRecord` is created
  from `Lost Time (hr) = 6.5` — but the half-typed shape is an undecided decision (model the columns,
  or stop parsing). No migration was written for either option.
* **Not attempted this session:** the review surface, timeline kinds, the lifecycle deletion
  regression matrix as a committed test, `LOGGING`/`SERVICE_REPORT` readiness audits, mutation
  probes, the performance pass, the full suite, release smoke, clean install and CI. None of these
  may be assumed done.
* A clock time (`08:20`) is not parsed by the shared ISO contract; it survives as source text only.
  Widening `_iso` would silently change every domain, so it was left alone and the fixtures state
  date-only ISO.

ADRs 27–31 appended to `docs/DECISIONS.md`.
