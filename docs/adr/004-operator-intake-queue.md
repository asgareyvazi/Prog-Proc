# ADR 004 — Local-first operator intake queue

- **Status:** Accepted (2026-11-01)
- **Context:** V10.3 Phase 4
- **Decides:** How manually selected source files enter an auditable review inbox without becoming domain facts.
- **Scope:** PDF, XLSX/XLSM, DOCX, CSV, and TXT only.

## Decision

The workspace stores only the canonical source path, filename, extension, byte size, measured SHA-256, queue state, actor, timestamp, and human reason. It never copies or stores the file bytes. A unique digest within the workspace database prevents duplicate queue entries; a content change requires a new queue entry.

Preview re-checks the measured digest, then uses only the existing built-in extractors, deterministic classifier, and static promotion-contract registry. Preview results are transient. No AI/provider or network probe runs, and no `Document`, extraction, knowledge, promotion, calculation, or operational row is written. Provenance and classification evidence are shown as evidence, not as approval.

A reviewer must supply a non-empty actor and reason to mark an item `REVIEWED` or `REJECTED`. A disposition is immutable. Every enqueue and disposition appends an `AuditEvent`; stale source bytes fail closed and leave the item queued.

The queue is not a document-class writer, a publication pipeline, or a substitute for `ReviewActionService`. A future explicit registration action must use the existing `DocumentRegistry`/`IngestionPipeline` and preserve their current gates; this ADR does not authorize a new writer. M2–M5 remain owner-gated and unchanged.

## Addendum — explicit registry registration (2026-11-02)

The post-V10.3 mission adds the requested explicit `REGISTERED` step without changing this ADR's writer boundary. A registration action is available only after a human `REVIEWED` disposition, revalidates the same measured source bytes, delegates to the existing `DocumentRegistry`, and stores an FK-protected registration record plus audit in the same unit of work. It creates document/version evidence only; it does not promote knowledge, calculations, or operational records. `REGISTERED` is a derived presentation state; the persisted queue disposition remains `REVIEWED` and `intake_registration` is the separate registration fact. Migration `0025` and `tests/integration/test_intake_registration.py` cover the lifecycle and fail-closed downgrade behavior. This addendum supersedes the earlier sentence that registration was future work; all other refusal boundaries remain.

## Alternatives rejected

- Automatically promoting a file from its extension or classifier: rejected; classification is not authority.
- Persisting extracted text in the inbox: rejected; this would create a parallel evidence store.
- Invoking an AI or remote classifier during preview: rejected; local-first and fail-closed behavior remain.
- Treating offscreen UI tests as Windows physical-display acceptance: rejected.

## Consequences

- Migration `0024` adds `intake_queue_item`; the queue remains in the workspace's system-of-record SQLite database.
- Preview performs no write and is revalidated against the original content hash.
- Human disposition records actor, time, reason, and append-only audit evidence.
- Existing class counts, static contracts, writers, and owner decisions do not change.
