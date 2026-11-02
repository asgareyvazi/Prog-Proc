"""One small Qt worker boundary for review loads and optional citation auditing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..core.errors import (
    DrillingIntelligenceError,
    IntegrationUnavailableError,
    ParserUnavailableError,
    ProviderUnavailableError,
    ValidationError,
)
from ..database.integrity import KnowledgeIntegrityError
from ..review import ReviewActionRequest
from .controller import ReviewController
from .errors import WorkerError


class IntakeWorker(QObject):
    """Run one bounded intake queue operation outside the GUI event loop."""

    succeeded = Signal(str, object)
    failed = Signal(str, object)

    def __init__(
        self, controller: ReviewController, operation: str, payload: dict[str, Any]
    ) -> None:
        super().__init__()
        self._controller = controller
        self._operation = operation
        self._payload = payload

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                self._operation,
                WorkerError("cancelled", "Intake operation cancelled before start."),
            )
            return
        try:
            if self._operation == "list":
                result = self._controller.intake_items(status=self._payload.get("status", ""))
            elif self._operation == "enqueue":
                result = self._controller.enqueue_intake(
                    self._payload["paths"], actor=self._payload["actor"]
                )
            elif self._operation == "preview":
                result = self._controller.preview_intake(self._payload["item_id"])
            elif self._operation == "decide":
                result = self._controller.decide_intake(
                    self._payload["item_id"],
                    decision=self._payload["decision"],
                    actor=self._payload["actor"],
                    reason=self._payload["reason"],
                )
            elif self._operation == "register":
                result = self._controller.register_intake(
                    self._payload["item_id"],
                    actor=self._payload["actor"],
                    reason=self._payload["reason"],
                    well_id=self._payload.get("well_id", ""),
                )
            else:
                raise ValueError(f"unknown intake operation: {self._operation}")
        except Exception as exc:  # noqa: BLE001 - thread boundary reports failures without hiding them
            self.failed.emit(
                self._operation,
                WorkerError(
                    "intake",
                    str(exc),
                    "Check the source and workspace; no result was accepted.",
                    type(exc).__name__,
                ),
            )
            return
        self.succeeded.emit(self._operation, result)


class DdrWorker(QObject):
    """Run one DDR authoring command in a worker, returning plain domain values."""

    succeeded = Signal(str, object)
    failed = Signal(str, object)

    def __init__(
        self, controller: ReviewController, operation: str, payload: dict[str, Any]
    ) -> None:
        super().__init__()
        self._controller = controller
        self._operation = operation
        self._payload = payload

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                self._operation,
                WorkerError("cancelled", "DDR operation cancelled before start."),
            )
            return
        try:
            if self._operation == "list":
                result = self._controller.ddr_reports(
                    self._payload["well_id"],
                    include_history=self._payload.get("include_history", True),
                )
            elif self._operation == "get":
                result = self._controller.ddr_report(self._payload["report_id"])
            elif self._operation == "create":
                result = self._controller.ddr_create_draft(**self._payload)
            elif self._operation == "save":
                result = self._controller.ddr_save_draft(
                    self._payload["report_id"], **self._payload["values"]
                )
            elif self._operation == "submit":
                result = self._controller.ddr_submit(
                    self._payload["report_id"], **self._payload["values"]
                )
            elif self._operation == "revision":
                result = self._controller.ddr_create_revision(
                    self._payload["report_id"], **self._payload["values"]
                )
            else:
                raise ValueError(f"unknown DDR operation: {self._operation}")
        except Exception as exc:  # noqa: BLE001 - report unexpected worker failures to the UI
            self.failed.emit(
                self._operation,
                WorkerError(
                    "ddr",
                    str(exc),
                    "Check the selected report and workspace; no action was accepted.",
                    type(exc).__name__,
                ),
            )
            return
        self.succeeded.emit(self._operation, result)


class ReviewWorker(QObject):
    """Run one read-only review outside the GUI event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        controller: ReviewController,
        well_id: str,
        lifecycle: str,
        verify_citations: bool,
        limit: int = 0,
        *,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._well_id = well_id
        self._lifecycle = lifecycle
        self._verify_citations = verify_citations
        self._limit = limit

    @staticmethod
    def _error(category: str, exc: DrillingIntelligenceError, default_hint: str) -> WorkerError:
        return WorkerError(
            category,
            str(exc),
            str(exc.context.get("hint") or exc.hint or default_hint),
            type(exc).__name__,
        )

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "Review load was cancelled before it started.")
            )
            return
        try:
            review = self._controller.load_review(
                self._well_id,
                lifecycle=self._lifecycle,
                verify_citations=self._verify_citations,
                limit=self._limit,
            )
        except ValidationError as exc:
            self.failed.emit(
                self._error("input", exc, "Check the selected well and review options.")
            )
        except KnowledgeIntegrityError as exc:
            self.failed.emit(
                self._error("integrity", exc, "Run the existing integrity checks before reviewing.")
            )
        except (
            ParserUnavailableError,
            ProviderUnavailableError,
            IntegrationUnavailableError,
        ) as exc:
            self.failed.emit(
                self._error("unavailable", exc, "Check the configured source or integration.")
            )
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                self._error("domain", exc, "Review the existing domain error details.")
            )
        except OSError as exc:
            self.failed.emit(
                WorkerError(
                    "unavailable",
                    str(exc),
                    "Check the workspace or cited source file.",
                    type(exc).__name__,
                )
            )
        except Exception as exc:  # noqa: BLE001  # preserve a visible UI boundary for unexpected failures
            exception_type = type(exc).__name__
            if exception_type == "IntegrityError":
                category = "integrity"
                hint = "The authoritative database reported an integrity failure."
            elif exception_type in {"OperationalError", "DatabaseError"}:
                category = "unavailable"
                hint = "The authoritative database is unavailable; check the workspace path."
            else:
                category = "unexpected"
                hint = "Enable debug logging for the underlying exception."
            self.failed.emit(
                WorkerError(
                    category,
                    str(exc) or "The review could not be loaded.",
                    hint,
                    exception_type,
                )
            )
        else:
            self.succeeded.emit(review)


class DecisionWorker(QObject):
    """Run one read-only decision-pack build outside the GUI event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, controller: ReviewController, well_id: str, *, parent: Any = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._well_id = well_id

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "Decision pack load was cancelled before it started.")
            )
            return
        try:
            payload = self._controller.decision(self._well_id)
        except ValidationError as exc:
            self.failed.emit(
                WorkerError(
                    "input",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "Check the selected well."),
                    type(exc).__name__,
                )
            )
            return
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                WorkerError(
                    "workspace",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "The workspace could not answer."),
                    type(exc).__name__,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - the GUI boundary reports, it does not crash
            self.failed.emit(WorkerError("internal", str(exc), "", type(exc).__name__))
            return
        self.succeeded.emit(payload)


class ComparisonWorker(QObject):
    """Run one read-only cross-well comparison build outside the GUI event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(
        self, controller: ReviewController, well_ids: Sequence[str], *, parent: Any = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._well_ids = tuple(well_ids)

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "Comparison pack load was cancelled before it started.")
            )
            return
        try:
            payload = self._controller.compare(list(self._well_ids))
        except ValidationError as exc:
            self.failed.emit(
                WorkerError(
                    "input",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "Select at least two wells."),
                    type(exc).__name__,
                )
            )
            return
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                WorkerError(
                    "workspace",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "The workspace could not answer."),
                    type(exc).__name__,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - the GUI boundary reports, it does not crash
            self.failed.emit(WorkerError("internal", str(exc), "", type(exc).__name__))
            return
        self.succeeded.emit(payload)


class ReportWorker(QObject):
    """Run one read-only report build outside the GUI event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        controller: ReviewController,
        *,
        well_ids: Sequence[str] = (),
        anchor: str = "",
        offsets: Sequence[str] = (),
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._well_ids = tuple(well_ids)
        self._anchor = anchor
        self._offsets = tuple(offsets)

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "Report build was cancelled before it started.")
            )
            return
        try:
            payload = self._controller.report(
                well_ids=list(self._well_ids),
                anchor=self._anchor,
                offsets=list(self._offsets),
            )
        except ValidationError as exc:
            self.failed.emit(
                WorkerError(
                    "input",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "Check the selected wells."),
                    type(exc).__name__,
                )
            )
            return
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                WorkerError(
                    "workspace",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "The workspace could not answer."),
                    type(exc).__name__,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - the GUI boundary reports, it does not crash
            self.failed.emit(WorkerError("internal", str(exc), "", type(exc).__name__))
            return
        self.succeeded.emit(payload)


class ReportAuditWorker(QObject):
    """Verify citations on a loaded report. Does not rebuild it."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, controller: ReviewController, payload: dict, *, parent: Any = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._payload = payload

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(WorkerError("cancelled", "Citation verification was cancelled."))
            return
        try:
            result = self._controller.audit_report(self._payload)
        except Exception as exc:  # noqa: BLE001 - the GUI boundary reports, it does not crash
            self.failed.emit(WorkerError("internal", str(exc), "", type(exc).__name__))
            return
        self.succeeded.emit(result)


class CalculationWorker(QObject):
    """Run one explicit NPT V1 calculation outside the GUI event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        controller: ReviewController,
        well_id: str,
        actor: str,
        supersedes_id: str = "",
        *,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._well_id = well_id
        self._actor = actor
        self._supersedes_id = supersedes_id

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "The calculation was cancelled before it started.")
            )
            return
        try:
            result = self._controller.run_npt_rollup(
                self._well_id,
                created_by=self._actor,
                supersedes_id=self._supersedes_id,
            )
        except ValidationError as exc:
            self.failed.emit(
                WorkerError(
                    "calculation",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "Check the promoted NPT evidence."),
                    type(exc).__name__,
                )
            )
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                WorkerError(
                    "domain",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "The calculation was rejected."),
                    type(exc).__name__,
                )
            )
        except OSError as exc:
            self.failed.emit(
                WorkerError(
                    "unavailable",
                    str(exc),
                    "The authoritative database is unavailable; check the workspace path.",
                    type(exc).__name__,
                )
            )
        except Exception as exc:  # noqa: BLE001 - preserve an explicit visible worker boundary
            exception_type = type(exc).__name__
            category = (
                "integrity"
                if exception_type == "IntegrityError"
                else "unavailable"
                if exception_type in {"OperationalError", "DatabaseError"}
                else "unexpected"
            )
            hint = (
                "The authoritative database reported an integrity failure."
                if category == "integrity"
                else "The authoritative database is unavailable; check the workspace path."
                if category == "unavailable"
                else "Enable debug logging for the underlying exception."
            )
            self.failed.emit(WorkerError(category, str(exc), hint, exception_type))
        else:
            self.succeeded.emit(result)


class ReviewActionWorker(QObject):
    """Run one confirmed domain action outside the GUI event loop.

    The worker receives an immutable request containing the displayed preconditions.  It never
    updates widgets or a review value itself; success is followed by a fresh DomainReview read by
    the window, while failure leaves the displayed review untouched.
    """

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        controller: ReviewController,
        request: ReviewActionRequest,
        *,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._request = request

    @Slot()
    def run(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
            self.failed.emit(
                WorkerError("cancelled", "The review action was cancelled before it started.")
            )
            return
        try:
            result = self._controller.execute_action(self._request)
        except ValidationError as exc:
            self.failed.emit(
                WorkerError(
                    "action",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "Reload the review and try again."),
                    type(exc).__name__,
                )
            )
        except DrillingIntelligenceError as exc:
            self.failed.emit(
                WorkerError(
                    "domain",
                    str(exc),
                    str(exc.context.get("hint") or exc.hint or "The domain rejected this action."),
                    type(exc).__name__,
                )
            )
        except OSError as exc:
            self.failed.emit(
                WorkerError(
                    "unavailable",
                    str(exc),
                    "The authoritative database is unavailable; check the workspace path.",
                    type(exc).__name__,
                )
            )
        except Exception as exc:  # noqa: BLE001 - preserve a visible worker boundary
            exception_type = type(exc).__name__
            category = (
                "integrity"
                if exception_type == "IntegrityError"
                else "unavailable"
                if exception_type in {"OperationalError", "DatabaseError"}
                else "unexpected"
            )
            hint = (
                "The authoritative database reported an integrity failure."
                if category == "integrity"
                else "The authoritative database is unavailable; check the workspace path."
                if category == "unavailable"
                else "Enable debug logging for the underlying exception."
            )
            self.failed.emit(WorkerError(category, str(exc), hint, exception_type))
        else:
            self.succeeded.emit(result)


__all__ = [
    "CalculationWorker",
    "ComparisonWorker",
    "DecisionWorker",
    "ReportWorker",
    "ReviewActionWorker",
    "ReviewWorker",
    "WorkerError",
]
