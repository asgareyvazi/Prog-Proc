"""One small Qt worker boundary for review loads and optional citation auditing."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class WorkerError:
    """Structured UI error without exposing a traceback to the operator."""

    category: str
    message: str
    hint: str = ""
    exception_type: str = ""


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


__all__ = ["ReviewActionWorker", "ReviewWorker", "WorkerError"]
