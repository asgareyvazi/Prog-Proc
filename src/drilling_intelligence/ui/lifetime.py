"""Destroy Qt windows while QApplication is still alive.

PySide 6.12 aborts in ``destroyQCoreApplication`` when a top-level widget is
still alive at interpreter shutdown. ``close()`` only hides a window, and a
refused close means a worker is still using it. A refused close stays valid.
The caller must not quit or return while that widget is a live Qt object.

One monotonic budget covers close, worker join, polling, and ``processEvents``.
The wait between attempts is ``QThread.msleep`` of the time still left, so a
quiet loop cannot block past the deadline. There is no local ``QEventLoop``
whose deadline is checked only after it returns. Widget destruction uses
``shiboken6.delete``, not ``deleteLater``, so cleanup does not depend on a
deferred-delete event. Worker completion is observed by ``QThread.wait``.

``aboutToQuit`` is not a safe place to join a worker or delete a window. That
signal runs inside the quit sequence, and a blocking join there can deadlock
if the worker still needs events. Supported routes are the ones that return
from ``QApplication.exec()`` — ``quit()``, ``exit()``, or the last window
closing — and then ``application_shutdown``. A platform or library path that
destroys the process without returning from ``exec()`` is not intercepted.
A ``quit()`` issued before ``exec()`` has started has no effect and is not
evidence of an external shutdown. ``QApplication`` also ignores a quit that
has started when a visible window refuses ``close()``, so ``exec()`` would
never return and this budget would never start. ``create_application`` accepts
that quit without closing windows inside the quit sequence.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

# Harness-only failure. The fixture uses it after a bounded wait still finds a
# live widget, so Py_Finalize does not turn that failure into the historical
# native abort. It is not a successful teardown.
FIXTURE_INCOMPLETE_EXIT = 3
SHUTDOWN_INCOMPLETE_EXIT = 4
# Ceiling for a refused close after exec() has already returned. Distinct from
# the 10s worker join inside MainWindow.closeEvent. The join is not restarted,
# and this ceiling is not restarted, on each poll.
PRODUCTION_SHUTDOWN_WAIT_MS = 120_000
_POLL_SLICE_MS = 50


def remaining_budget_ms(deadline_ms: int, elapsed_ms: int) -> int:
    """Time left on the one shutdown clock. Negative means the budget is spent."""
    return deadline_ms - elapsed_ms


def worker_join_limit_ms(configured_ms: int, remaining_ms: int) -> int:
    """Keep the configured join when the shutdown budget still covers it.

    The last attempt must not start a join longer than the time still left.
    ``wait(0)`` does not block, so a worker that has already finished can still
    be released on the deadline without extending it.
    """
    return min(configured_ms, max(0, remaining_ms))


def wait_slice_ms(remaining_ms: int, *, slice_ms: int = _POLL_SLICE_MS) -> int:
    """A poll interval that cannot outlive the remaining budget."""
    return min(slice_ms, max(0, remaining_ms))


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of one release pass. ``complete`` is validity, not a call count."""

    released: int
    refused: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.refused


def _apply_join_budget(widget: Any, remaining_ms: int) -> None:
    configured = getattr(widget, "_worker_join_timeout_ms", None)
    if not isinstance(configured, int):
        return
    widget._shutdown_join_budget_ms = worker_join_limit_ms(configured, remaining_ms)


def _clear_join_budget(widget: Any) -> None:
    import shiboken6

    if shiboken6.isValid(widget) and hasattr(widget, "_shutdown_join_budget_ms"):
        del widget._shutdown_join_budget_ms


def _pump(application: Any, budget_ms: int | None) -> None:
    """Deliver pending events without opening a loop that can block past the budget.

    A bare ``processEvents(int)`` would be read as flags, not as a time limit.
    The timed overload is the one that returns when the allowance is spent.
    """
    if budget_ms is not None and budget_ms <= 0:
        return
    if budget_ms is None:
        application.processEvents()
        return
    from PySide6.QtCore import QEventLoop

    application.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, budget_ms)


def release_top_level_widgets(
    application: Any,
    *,
    remaining_ms: int | None = None,
) -> ReleaseResult:
    """Close and delete widgets whose close was accepted.

    A widget that ignores ``close()`` is left valid. The returned names are the
    widgets that are still valid after the pass, checked with the wrapper, not
    the widgets we hoped to close. ``remaining_ms`` caps a worker join so the
    close cannot outlive the shutdown budget. ``None`` keeps the widget's own
    join, which is the direct-release test path.
    """
    import shiboken6
    from PySide6.QtWidgets import QApplication

    released = 0
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        # After exec() has returned, joining a still-running worker from close()
        # can fail to return on this host. Poll isRunning() instead, and close
        # only once the worker has finished. A user close while the loop is
        # still running still uses the 10s join inside closeEvent.
        running = getattr(widget, "owned_workers_running", None)
        if remaining_ms is not None and callable(running) and running():
            continue
        if remaining_ms is not None:
            _apply_join_budget(widget, remaining_ms)
        accepted = widget.close() is not False
        _clear_join_budget(widget)
        if not accepted:
            continue
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
            released += 1
    # A full-budget pump would restart the allowance already spent inside close().
    # Deletion is shiboken6.delete, so a short slice is enough to deliver
    # bookkeeping. The coordinator applies the remaining budget again afterwards.
    _pump(application, None if remaining_ms is None else wait_slice_ms(remaining_ms))
    refused: list[str] = []
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        refused.append(widget.objectName() or type(widget).__name__)
    return ReleaseResult(released, tuple(refused))


def fail_closed_shutdown(result: ReleaseResult) -> None:
    """Stop the process without returning into interpreter teardown.

    This is a failed shutdown, not a safe lifecycle. It runs only when the
    production deadline is spent and a widget is still valid. It does not
    delete that widget and it does not kill a worker by ``QThread.terminate``.
    """
    names = ", ".join(result.refused) or "unknown"
    print(
        f"Qt shutdown incomplete; live windows remain ({names})",
        file=sys.stderr,
    )
    os._exit(SHUTDOWN_INCOMPLETE_EXIT)


def _expired(
    code: int,
    result: ReleaseResult,
    on_expire: str,
) -> tuple[int, ReleaseResult]:
    if on_expire == "fail_closed":
        fail_closed_shutdown(result)
    return SHUTDOWN_INCOMPLETE_EXIT, result


def _coordinate(
    application: Any,
    code: int,
    *,
    deadline_ms: int,
    on_expire: str,
) -> tuple[int, ReleaseResult]:
    """Retry release until the widgets are gone or the one budget is spent.

    The timer is started once. Later iterations subtract from that clock;
    they do not open a fresh production ceiling.
    """
    from PySide6.QtCore import QElapsedTimer, QThread

    elapsed = QElapsedTimer()
    elapsed.start()
    while True:
        remaining = remaining_budget_ms(deadline_ms, int(elapsed.elapsed()))
        if remaining <= 0:
            result = release_top_level_widgets(application, remaining_ms=0)
            if result.complete:
                return code, result
            return _expired(code, result, on_expire)
        result = release_top_level_widgets(application, remaining_ms=remaining)
        if result.complete:
            return code, result
        remaining = remaining_budget_ms(deadline_ms, int(elapsed.elapsed()))
        pause = wait_slice_ms(remaining)
        if pause <= 0:
            return _expired(code, result, on_expire)
        QThread.msleep(pause)
        _pump(application, remaining_budget_ms(deadline_ms, int(elapsed.elapsed())))


def finish_qt_application(
    application: Any,
    code: int = 0,
    *,
    max_wait_ms: int | None = None,
    deadline_ms: int | None = None,
) -> tuple[int, ReleaseResult]:
    """Release every top-level widget before the caller may leave Qt.

    ``deadline_ms`` selects the production terminal: the same state machine as
    an ordinary ``drillintel-ui`` shutdown, with only the ceiling changed. The
    default ceiling is 120s and expiry calls ``fail_closed_shutdown``.
    ``max_wait_ms`` is the fixture path. It uses the same coordinator but
    returns an incomplete result instead of exiting, so the fixture can record
    exit 3 without hanging for the production ceiling. Passing both is rejected
    by using the production terminal only when ``max_wait_ms`` is omitted.
    """
    if max_wait_ms is not None:
        return _coordinate(
            application,
            code,
            deadline_ms=max_wait_ms,
            on_expire="incomplete",
        )
    ceiling = PRODUCTION_SHUTDOWN_WAIT_MS if deadline_ms is None else deadline_ms
    return _coordinate(
        application,
        code,
        deadline_ms=ceiling,
        on_expire="fail_closed",
    )


def create_application(argv: list[str]) -> Any:
    """The ``QApplication`` used by ``drillintel-ui`` and the shutdown proof.

    Qt delivers ``quit()`` as ``QEvent::Quit``. ``QApplication`` closes every
    visible window and ignores that event if one window remains. A worker that
    refuses ``close()`` would then leave ``exec()`` running forever. Accepting
    the quit only leaves the loop. ``application_shutdown`` releases windows
    afterwards and is allowed to fail closed. Closing them in this filter would
    re-enter the path Qt uses to cancel the quit.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject
    from PySide6.QtWidgets import QApplication

    class _AcceptQuit(QObject):
        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if event.type() == QEvent.Type.Quit:
                # Do not close windows here. A refused close makes Qt ignore
                # the quit, and the offscreen close path does not return while
                # a worker is still running.
                QCoreApplication.exit(0)
                return True
            return super().eventFilter(watched, event)

    application = QApplication(argv)
    accept_quit = _AcceptQuit(application)
    application.installEventFilter(accept_quit)
    # The Python wrapper must outlive the filter. Parenting alone is not enough
    # if the wrapper is collected and shiboken deletes the C++ object.
    application._accept_quit = accept_quit  # type: ignore[attr-defined]
    return application


def application_shutdown(
    application: Any,
    code: int = 0,
    *,
    deadline_ms: int | None = None,
) -> int:
    """The production path ``drillintel-ui`` runs after ``QApplication.exec``.

    ``deadline_ms`` changes only the duration. The terminal failure route stays
    process exit 4. A successful release returns ``code`` and does not quit
    again; the caller already left ``exec()``.
    """
    exit_code, result = finish_qt_application(application, code, deadline_ms=deadline_ms)
    if not result.complete:
        fail_closed_shutdown(result)
    return exit_code


def fixture_teardown(application: Any, *, max_wait_ms: int = 15_000) -> None:
    """Session teardown. Do not quit while a top-level widget is still valid."""
    _code, result = finish_qt_application(application, 0, max_wait_ms=max_wait_ms)
    if not result.complete:
        print(
            "Qt fixture teardown incomplete; refusing to quit with live windows",
            file=sys.stderr,
        )
        os._exit(FIXTURE_INCOMPLETE_EXIT)
    application.quit()
    application.processEvents()
