# Human Review & Governed Decision Actions V1 — A–Q Forensic Report

Date: 2026-09-19  
Branch: `arena/01a0b7fd-prog-proc`  
Repository: `asgareyvazi/Prog-Proc`  
Baseline head: `921f89be4fdb03421cf02fec17011d4bbbd28368`  
Implementation head: `9c233bd` (`Implement governed human review actions`)  

## A. Authority and scope

SQLite/domain repositories remain the system of record. `DomainReviewService` remains a read-only
projection for one well. The action phase covers lessons, procedures, programs, best practices,
recommendations, risks, operational confirmation records, field patterns, cost lines, and knowledge
conflict decisions. The complete transition/actor/reason/evidence/self-approval/audit/idempotence table
is in [`HUMAN_REVIEW_ACTION_INVENTORY.md`](HUMAN_REVIEW_ACTION_INVENTORY.md).

## B. Implemented architecture

The write path is now:

```text
authoritative SQLite row
  -> DomainReviewService / ReviewRecord or ReviewConflict
  -> domain-derived ReviewAction capability
  -> immutable ReviewActionRequest
  -> ReviewActionService re-read + stale/scope checks
  -> owning repository/service method
  -> existing status/approval/history/audit metadata
  -> committed ReviewActionResult
  -> fresh DomainReview reload in Qt
```

No review-action, approval, decision, workflow, or duplicate audit table was added.

## C. Inventory result

`docs/HUMAN_REVIEW_ACTION_INVENTORY.md` records every inspected human decision path, including
paths intentionally not exposed when the existing owner cannot represent actor/reason/audit semantics.
The inventory explicitly calls out `WellRepository.set_well_status`, document processing status,
internal knowledge status maintenance, calculations, and mechanical revision/promotion operations.

## D. Domain defects corrected

* Repeated procedure approval no longer rewrites `approved_by`, `approved_at`, `reviewer`, or
  `reviewed_at`.
* Repeated program approval no longer rewrites `approver` or `approved_at`.
* Repeated lesson and best-practice approval no longer rewrites approval metadata.
* Repeated recommendation decisions no longer rewrites decision actor/time or decline reason.
* Same-state lesson submission no longer back-fills reviewer metadata.
* Same-state program review submission no longer back-fills `submitted_at`.
* Knowledge conflict resolution requires an explicit actor; a repeated decision for the same winner is
  read-only and does not append another audit event; replacing a settled winner is rejected.
* The CLI no longer invents the actor `operator` when resolving a conflict.

The shared `set_record_status` helper was not changed globally because repeated pattern confirmation is
an existing, tested safe no-op.

## E. Lifecycle and idempotence semantics

Capabilities are derived from the existing `Lifecycle` machines and a narrow domain allow-list. Legal
edges are preserved; generic status buttons do not expose revision supersession. Same-state retries are
not presented as new actions, and the owning approval/decision methods are safe when called directly.
Operational, pattern, and cost history continues to use the existing structured
`attributes.status_history`; procedure/lesson/risk history continues to use the existing status note.

## F. Actor, reason, evidence, and self-approval

Every V1 action request carries an explicit actor. Domain-specific rules remain in their owners:
lesson approval requires recorded evidence and a non-author approver; best-practice approval requires a
rationale and a non-author approver; lesson rejection and recommendation decline require reasons; risk
mitigation/closure require reasons; conflict resolution requires a recorded candidate and actor. No
readiness, confidence, quality, safety, variance, or risk score was invented.

## G. Revision, scope, and stale protections

`ReviewActionRequest` is frozen and carries displayed expected status, optional exact revision/current
flag, expected scope, selected well, and authoritative `updated_at`. Execution opens a single unit of
work, refreshes/locks the exact row where supported, revalidates all supplied preconditions, rejects
historical/superseded rows and scope mismatches, and only then calls the existing owner. A stale or
wrong-revision request fails before mutation with a reload hint. Stale field-pattern snapshots are not
actionable until refreshed.

## H. Conflict decisions

Conflict resolution routes through `KnowledgeExtractionService.resolve`, which remains the owner of
candidate retirement, re-comparison, and the existing append-only `knowledge.conflict_resolved` audit
event. The action boundary does not duplicate that audit system. The chosen fact becomes active and
other candidates remain stored as retired history.

## I. Action boundary API

`review/actions.py` provides frozen value objects:

* `ReviewAction` / `ActionCapability`
* `ReviewActionRequest` / `ActionRequest`
* `ReviewActionResult` / `ActionResult`
* `ReviewActionService`
* `ReviewActionError`

The service exposes `available_actions`/`capabilities`/`actions_for` for read-side capability discovery
and `execute`/`execute_action` for the explicit write boundary. It owns no persistence and routes only
to existing domain methods.

## J. Domain mutation preservation

The boundary routes to `EngineeringRepository`, `LessonRepository`, `RiskRepository`,
`OperationsRepository`, `CostRepository`, `IntelligenceService`, and `KnowledgeExtractionService`.
Approval fields, lifecycle transitions, evidence/provenance, revision chains, UTC domain timestamps,
status history, conflict candidates, and append-only audit events remain owned by those domains.

## K. Desktop workbench integration

The optional Qt workbench now renders action capabilities for selected current records and open
conflicts. It collects actor and reason/note input, uses semantic confirmation dialogs, creates an
immutable request, and never writes from a widget or directly edits `DomainReview`.

## L. Worker, duplicate, and failure behavior

`ReviewActionWorker` runs the action outside the GUI thread. The window disables action controls while
a submission is in flight, ignores duplicate/delayed responses, and waits for workers on close. Success
never patches the displayed DTO: it starts a fresh authoritative `DomainReview` load. Failure leaves the
existing review/detail selection visible and shows the domain error. Dialog opening and cancellation
are read-only; only the confirmed worker request can write.

## M. Test gates

Final collected suite: **1077 tests collected; 1074 passed; 3 skipped**. The full suite passed with
`/tmp/proc-venv/bin/python -m pytest -q`.

Focused gates also passed:

* `tests/unit/test_review_actions.py` — immutable requests, capability gating, stale rejection,
  repository routing, and approval metadata replay.
* engineering lesson/program/procedure lifecycle tests;
* intelligence pattern staleness/confirmation tests;
* knowledge conflict and CLI tests;
* Domain Review, operational promotion, program promotion, integrity, audit-policy, and race-safety
  suites;
* full `ruff check src tests` and `compileall`.

## N. Packaging and installed artifact

A wheel was built successfully with the existing package metadata. A clean artifact environment
installed the wheel with runtime dependencies, passed `pip check`, imported the new review action API,
and confirmed `PySide6.QtWidgets` was not imported. The optional Qt extra remains unchanged and is not
part of the default install.

## O. Mutation/safety checks

No third-party mutation runner was present in the checkout. The safety mutation set was exercised by
explicit regression tests and the full race/integrity gate: changing same-state approval behavior,
removing expected-status checks, bypassing actor/evidence/reason checks, or allowing a second conflict
decision is covered by a failing assertion in the focused action/domain tests. No source mutation was
left in the worktree.

## P. Environment and intentional boundaries

Real Qt execution remains skipped in this host because `PySide6.QtWidgets` cannot load the host's
`libGL.so.1`; UI code was compiled, linted, and covered by the existing skip-aware UI suite. Well
lifecycle mutation is intentionally not exposed in V1 because the existing `set_well_status` method
has no actor/reason/history contract. No migration was needed.

## Q. Final disposition

Human Review & Governed Decision Actions V1 is implemented on the fixed Arena branch. The read boundary
is still read-only, confirmed writes are domain-owned, stale/revision/scope protections are explicit,
repeat approvals preserve attribution, the workbench uses a worker/action boundary, and the final
headless quality and packaging gates pass.
