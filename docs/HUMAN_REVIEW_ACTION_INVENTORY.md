# Human Review & Governed Decision Actions V1

This is the V1 forensic inventory of human mutation paths reachable from the authoritative domain. It is
kept next to the action boundary so adding a new domain decision cannot be mistaken for adding a button.
The `DomainReviewService` remains read-only. The only V1 write boundary is `review/actions.py`, which
re-reads the exact row and delegates to the owning repository/service listed below.

## Governed action inventory

| Authoritative table / domain method | Transition(s) | Actor | Reason / evidence | Self-approval | Audit metadata | Idempotence / stale semantics | V1 review action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `procedure_record` / `EngineeringRepository.set_procedure_status` and `approve_procedure` | `DRAFT -> IN_REVIEW`, `DRAFT/IN_REVIEW -> APPROVED`, `IN_REVIEW -> DRAFT`, `DRAFT/IN_REVIEW/APPROVED -> WITHDRAWN` | Required for a real lifecycle edge; `approve_procedure` requires a non-blank `by` | Approval note is retained in the shared status history text; recorded provenance is carried by the row but the existing procedure method does not impose an evidence gate | No procedure-specific author check exists in the owning method | `status_note`, `approved_by`, `approved_at`, `reviewer`, `reviewed_at`; UTC domain timestamps | Same-state approval now returns without rewriting approver/timestamps. Action requests require status, current revision, scope and `updated_at` when displayed | Submit, approve, return to draft, withdraw |
| `drilling_program` / `EngineeringRepository.set_program_status` and `approve_program` | `DRAFT -> IN_REVIEW`, `DRAFT/IN_REVIEW -> APPROVED`, `IN_REVIEW -> DRAFT`, `DRAFT/IN_REVIEW -> ARCHIVED`, `APPROVED -> ARCHIVED` | Required for a real lifecycle edge; approval requires a non-blank `by` | Approval note is recorded in shared status history; no additional evidence gate is invented | No program-specific author check exists in the owning method | `status_note`, `submitted_at`, `approver`, `approved_at` | Same-state approval preserves prior approver/timestamp; same-state `IN_REVIEW` does not back-fill `submitted_at` | Submit, approve, return to draft, archive |
| `lesson_learned` / `LessonRepository.submit_for_review` | `DRAFT -> REVIEW` | Required by the lifecycle helper for the real edge | No reason required; reviewer is recorded on transition | N/A for submission | `status_note`, `reviewer` | Same-state submission does not rewrite reviewer metadata | Submit for review |
| `lesson_learned` / `LessonRepository.approve` | `DRAFT/REVIEW -> APPROVED` | Required and must not equal `created_by` | At least one recorded evidence edge/provenance entry is required | Author self-approval is rejected | `status_note`, `approved_by`, `approved_at`, `reviewer`, `reviewed_at` | Same-state approval is a read-only retry and preserves all approval metadata; stale revision/status/scope/update stamp is rejected before routing | Approve (only exposed when evidence is present) |
| `lesson_learned` / `LessonRepository.reject` and `reopen` | `DRAFT/REVIEW -> REJECTED`; `REJECTED -> DRAFT` | Required by lifecycle for both real edges | Rejection requires a non-blank reason; reopening may carry an optional reason | N/A | `status_note` retains actor, UTC timestamp and reason | Same-state transitions are not offered; stale requests are rejected | Reject, reopen |
| `best_practice` / `LessonRepository.approve_practice` | `DRAFT/IN_REVIEW -> APPROVED` | Required and must not equal `created_by` | Non-blank rationale is required; optional approval note is retained in status history | Author self-approval is rejected | `status_note`, `approved_by`, `approved_at`, `reviewer`, `reviewed_at` | Same-state approval preserves prior attribution/timestamp; revisions remain separate rows | Approve (only exposed when rationale exists) |
| `recommendation` / `LessonRepository.decide_recommendation` | `PROPOSED -> ACCEPTED/DECLINED/SUPERSEDED`, `ACCEPTED -> IMPLEMENTED/DECLINED/SUPERSEDED`, `DECLINED -> PROPOSED/SUPERSEDED`, `IMPLEMENTED -> SUPERSEDED` | Required for every decision | Decline requires a non-blank reason; other decisions carry an optional reason | No self-approval rule: recommendation generator and decision actor are separate fields | `status_note`, `decided_by`, `decided_at`, `decline_reason` | Repeating the same decision preserves actor/timestamp/reason; changed decisions still go through the lifecycle and overwrite only when the domain explicitly permits a new edge | Accept, decline, mark implemented, reopen, supersede |
| `risk_record` / `RiskRepository.set_risk_status` | `OPEN -> MITIGATED/CLOSED/SUPERSEDED`, `MITIGATED -> OPEN/CLOSED/SUPERSEDED`, `CLOSED -> OPEN` | Required for a real edge by the shared helper | Mitigation and closure require a non-blank reason; no risk score is computed | N/A | `status_note` with actor, UTC timestamp and reason | Lifecycle and reason validation remain in the risk repository; stale action requests are rejected | Mitigate, close, reopen, supersede |
| `ddr_report`, `well_operation`, `well_event`, `npt_record`, `problem_occurrence` / `OperationsRepository.set_status` | Confirmation machine: `CANDIDATE -> CONFIRMED/REJECTED`, `CONFIRMED -> CANDIDATE/REJECTED`, `REJECTED -> CANDIDATE` | Required for real transitions | Optional reason is kept in structured `attributes.status_history`; no evidence gate is invented because the owning method has none | N/A | Structured status history: actor, UTC `at`, from/to, reason | Same-state confirmation remains the existing safe no-op with no new history entry; UI actions are only offered for legal outgoing edges and require exact displayed preconditions | Confirm, reject, return to candidate |
| `field_pattern` / `set_pattern_status` via `IntelligenceService.confirm_pattern` | Same confirmation machine as operational records | Required for real transitions | Optional reason in `attributes.status_history` | N/A | Structured status history; pattern evidence/query remains on the pattern | Existing repeated same-state confirmation remains a no-op. V1 refuses actions against `stale_at` snapshots until the authoritative pattern is refreshed | Confirm, reject, return to candidate |
| `cost_item` / `CostRepository.set_status` | Same confirmation machine | Required for real transitions | Optional reason in structured status history; no currency calculation is performed | N/A | Structured status history | Same-state confirmation is a no-op; exact row/status/scope/update stamp is checked before routing | Confirm, reject, return to candidate |
| `knowledge_conflict` / `KnowledgeExtractionService.resolve` → `resolve_conflict` | `OPEN -> RESOLVED_MANUALLY` (the selected fact becomes `ACTIVE`, other candidates `RETIRED`) | Must be explicit; there is no implicit `operator` identity | Candidate id is mandatory; note is retained and is the human explanation | N/A | `KnowledgeConflict.resolution` plus the existing append-only `knowledge.conflict_resolved` audit event | Repeating an already-resolved conflict with the same winner is read-only; replacing a settled winner is rejected. Candidate, actor and audit writes are one transaction | Resolve conflict |

## Paths intentionally not exposed as V1 review actions

These are mutation paths found during the audit, but the existing method does not represent a governed
human decision with the required attribution/evidence semantics, or it is mechanical rather than a
review decision:

* `WellRepository.set_well_status` changes lifecycle but accepts no actor, reason or audit metadata;
  exposing it from the workbench would lose attribution. It remains a domain/API operation until its
  owner can represent the event without a migration or a second audit system.
* `DocumentRepository.set_document_status` changes ingestion pipeline state, not human approval state.
* `KnowledgeRepository.set_status` is an internal extraction/conflict-maintenance operation and has no
  actor/audit contract; human conflict decisions go through `KnowledgeExtractionService.resolve`.
* Calculation status and promotion/revision methods are either deterministic/structural or require
  domain inputs that an action button must not invent. A calculation is not made `CHECKED` or
  `APPROVED` by V1 without an owning human decision method.
* Revision creation (`revise`, `revise_practice`, `revise_procedure`, `revise_program`) is a content
  authoring operation, not an approval click. Superseding a revision therefore is not exposed as a
  generic status action; the owning revision method remains the only route.

## Boundary invariants

1. A capability is derived from the existing lifecycle plus the domain-specific evidence/reason rules;
   the UI cannot manufacture an action name or target status.
2. A request contains an explicit actor and the displayed expected status. Where available it also
   carries exact revision, current flag, scope and authoritative `updated_at`.
3. Execution opens one database unit of work, re-reads the exact row, rejects stale/history/scope
   mismatches, then calls the owning domain method. There is no review-action table or second audit
   system.
4. The Qt workbench opens a semantic confirmation dialog and hands an immutable request to
   `ReviewActionWorker`. It never mutates a widget-owned row or writes from the GUI thread.
5. After success the workbench reloads `DomainReview`; after failure it leaves the displayed review
   unchanged and shows the domain error. Duplicate concurrent action submissions are disabled.
