# ADR 005 — Daily DDR authoring workspace

- **Status:** Accepted for the post-V10.3 mission scope (2026-11-02)
- **Context:** Daily operator authoring is separate from file ingestion, review, promotion, and engineering calculation.
- **Decides:** Provide a real local authoring lifecycle for a daily drilling report and its explicitly entered activities, linked to the existing operational record model.

## Decision

1. An operator creates and edits a `DRAFT` using required, canonical `YYYY-MM-DD` report date, recorded operation vocabulary, and optional source-worded report fields. Author, reason, report revision, and activity sequence are retained in provenance/audit. Optimistic `updated_at` preconditions prevent stale writes.
2. Draft activities are not yet `WellOperation` facts. On explicit submit, the service transaction creates corresponding `WellOperation` rows with `CANDIDATE` status and links the `DdrActivity` entries. No duration, depth, NPT category, total, or other engineering value is inferred from narrative text.
3. Submit moves `authoring_state` to `SUBMITTED`; it does not change the report/operation review status from `CANDIDATE`. Independent review uses the existing `ReviewActionService` only for a current submitted report. Review/confirmation is not inferred from authoring, the source document, or submission.
4. A revision is a new report revision, never an in-place rewrite of historical evidence. The old report and its operations become non-current; the new revision starts as a draft and requires its own review. Review state is not copied as approval.
5. Authoring is not a new document classification, extractor, promotion handler, calculation method, or source of engineering rules. Parsed DDR promotion contracts remain the only document-promotion route for imported content.
6. Migration `0026` persists authoring/revision/currentness and ordered activities. SQLite is the only qualified backend. Because SQLite cannot add a self-referential constraint to the historical report table by ordinary `ALTER TABLE`, migration and metadata bootstrap install SQLite triggers enforcing valid authoring state, revision references, and set-null behavior on deletion. PostgreSQL is not qualified by this ADR.

## Alternatives rejected

- Treating a report list/import table as an authoring workspace: rejected; operators need editable draft fields, save/submit, revision/history, and activity entry.
- Making submit equivalent to confirm/approve: rejected; submission is authoring readiness, and independent review remains explicit.
- Creating candidate operations from draft text before submit: rejected; drafts are not yet operational records.
- Inferring durations, depth, units, NPT, or totals from free text: rejected; no approved extraction method or engineering formula is introduced.
- Updating a reviewed or historical report in place: rejected; use a new revision with currentness/provenance.
- Adding a new domain calculation or automatic promotion: rejected; outside this decision and owner-gated where applicable.

## Consequences

- `DdrAuthoringService` is the transactional domain API; the UI must use its lifecycle and cannot write arbitrary fields directly. No separate DDR CLI is claimed here.
- `DdrActivity` provides ordered operator-authored text linked to resulting operations; provenance follows report revision/actor/reason.
- The review UI must gate actions to current submitted reports and reload after a successful action.
- `tests/integration/test_ddr_authoring.py`, migration integrity/downgrade tests, and Qt UI tests are part of the evidence. A skipped Qt test is not a pass; exact-SHA offscreen CI and physical display acceptance are separate.
