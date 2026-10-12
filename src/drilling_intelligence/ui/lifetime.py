"""Destroy Qt windows while QApplication is still alive.

PySide 6.12 aborts in ``destroyQCoreApplication`` when a top-level widget is
still alive at interpreter shutdown. ``close()`` only hides a window, and a
refused close means a worker is still using it. A refused close stays valid.
The caller must not quit or return while that widget is a live Qt object.
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
# Ceiling for a sticky quit after exec() has already returned. Distinct from
# the 10s worker join inside MainWindow.closeEvent. A quiet event loop does
# not use this ceiling.
PRODUCTION_SHUTDOWN_WAIT_MS = 120_000


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of one release pass. ``complete`` is validity, not a call count."""

    released: int
    refused: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.refused


def release_top_level_widgets(application: Any) -> ReleaseResult:
    """Close and delete widgets whose close was accepted.

    A widget that ignores ``close()`` is left valid. The returned names are the
    widgets that are still valid after the pass, checked with the wrapper, not
    the widgets we hoped to close.
    """
    import shiboken6
    from PySide6.QtWidgets import QApplication

    released = 0
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        if widget.close() is False:
            continue
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
            released += 1
    application.processEvents()
    refused: list[str] = []
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        refused.append(widget.objectName() or type(widget).__name__)
    return ReleaseResult(released, tuple(refused))


def fail_closed_shutdown(result: ReleaseResult) -> None:
    """Stop the process without returning into interpreter teardown.

    This is a failed shutdown, not a safe lifecycle. It runs only when a quit
    flag is already stuck and a widget is still valid after the production
    ceiling. It does not delete that widget and it does not kill a worker by
    ``QThread.terminate``.
    """
    names = ", ".join(result.refused) or "unknown"
    print(
        f"Qt shutdown incomplete; live windows remain ({names})",
        file=sys.stderr,
    )
    os._exit(SHUTDOWN_INCOMPLETE_EXIT)


def finish_qt_application(
    application: Any,
    code: int = 0,
    *,
    max_wait_ms: int | None = None,
) -> tuple[int, ReleaseResult]:
    """Release every top-level widget before the caller may leave Qt.

    ``max_wait_ms is None`` is the production contract. A refused close does not
    return: the event loop keeps running so the worker can finish and a later
    close can be accepted. A bounded wait is for the test harness. If that bound
    expires, the result is incomplete and the widgets that refused close are
    still valid. Neither path calls ``quit()`` or deletes a refused widget.
    """
    from PySide6.QtCore import QElapsedTimer, QEventLoop, QTimer

    result = release_top_level_widgets(application)
    if result.complete:
        return code, result
    if max_wait_ms is not None:
        elapsed = QElapsedTimer()
        elapsed.start()
        while elapsed.elapsed() < max_wait_ms:
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()
            result = release_top_level_widgets(application)
            if result.complete:
                return code, result
        return SHUTDOWN_INCOMPLETE_EXIT, result

    elapsed = QElapsedTimer()
    elapsed.start()
    while True:
        loop = QEventLoop()
        spin = QElapsedTimer()
        spin.start()
        loop.exec()
        # A sticky quit returns immediately and would otherwise hammer close().
        if spin.elapsed() < 5:
            if elapsed.elapsed() >= PRODUCTION_SHUTDOWN_WAIT_MS:
                fail_closed_shutdown(result)
            pause = QEventLoop()
            QTimer.singleShot(200, pause.quit)
            pause.exec()
        result = release_top_level_widgets(application)
        if result.complete:
            return code, result


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
