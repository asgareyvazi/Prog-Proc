"""Subprocess body for the Qt shutdown contract. Not a pytest module."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _drop_thread(thread: object) -> None:
    import shiboken6

    if thread.isRunning():  # type: ignore[attr-defined]
        thread.wait()  # type: ignore[attr-defined]
    if shiboken6.isValid(thread):
        shiboken6.delete(thread)


def refused() -> int:
    import shiboken6
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication, QWidget

    from drilling_intelligence.ui.lifetime import release_top_level_widgets
    from drilling_intelligence.ui.main_window import MainWindow

    class Sleeper(QThread):
        def run(self) -> None:
            self.msleep(250)

    app = QApplication(["shutdown-refused"])
    other = QWidget()
    other.setObjectName("other-window")
    other.show()
    window = MainWindow()
    window.setObjectName("workbench")
    window._worker_join_timeout_ms = 40
    thread = Sleeper()
    window._review_thread = thread
    thread.start()
    window.show()
    first = release_top_level_widgets(app)
    if first.complete or not shiboken6.isValid(window):
        print("FAIL refused close deleted the live window", first.refused, file=sys.stderr)
        return 1
    if "workbench" not in first.refused:
        print("FAIL refused window was not reported", first.refused, file=sys.stderr)
        return 1
    if shiboken6.isValid(other):
        print("FAIL an accepted sibling was not deleted", file=sys.stderr)
        return 1
    print("REFUSED_KEPT_ALIVE")
    thread.wait()
    second = release_top_level_widgets(app)
    if not second.complete or shiboken6.isValid(window):
        print("FAIL cleanup after the worker finished", second.refused, file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("SHUTDOWN_OK")
    return 0


def external_quit() -> int:
    """quit() must not destroy a window whose worker is still running.

    A zero-timer inside exec() did not stop the loop on the CI host, so this
    calls quit() directly. That is the same call an external owner would make.
    """
    import shiboken6
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from drilling_intelligence.ui.lifetime import release_top_level_widgets
    from drilling_intelligence.ui.main_window import MainWindow

    class Sleeper(QThread):
        def run(self) -> None:
            self.msleep(200)

    app = QApplication(["shutdown-quit"])
    window = MainWindow()
    window.setObjectName("workbench")
    window._worker_join_timeout_ms = 40
    thread = Sleeper()
    window._review_thread = thread
    thread.start()
    window.show()
    app.processEvents()
    app.quit()
    if not shiboken6.isValid(window):
        print("FAIL quit destroyed the live window", file=sys.stderr)
        return 1
    first = release_top_level_widgets(app)
    if first.complete or not shiboken6.isValid(window) or "workbench" not in first.refused:
        print("FAIL quit bypassed a refused close", first.refused, file=sys.stderr)
        return 1
    print("QUIT_KEPT_ALIVE")
    if not thread.wait(2000):
        print("FAIL worker did not finish after quit", file=sys.stderr)
        return 1
    second = release_top_level_widgets(app)
    if not second.complete or shiboken6.isValid(window):
        print("FAIL cleanup after quit", second.refused, file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("EXTERNAL_QUIT_OK")
    return 0


def bounded_incomplete() -> int:
    import shiboken6
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from drilling_intelligence.ui.lifetime import SHUTDOWN_INCOMPLETE_EXIT, finish_qt_application
    from drilling_intelligence.ui.main_window import MainWindow

    class Sleeper(QThread):
        def run(self) -> None:
            self.msleep(5000)

    app = QApplication(["shutdown-bound"])
    window = MainWindow()
    window.setObjectName("workbench")
    window._worker_join_timeout_ms = 30
    thread = Sleeper()
    window._review_thread = thread
    thread.start()
    window.show()
    code, result = finish_qt_application(app, 0, max_wait_ms=80)
    if code != SHUTDOWN_INCOMPLETE_EXIT or result.complete or not shiboken6.isValid(window):
        print("FAIL bounded wait declared success", code, result.refused, file=sys.stderr)
        return 1
    print("BOUNDED_INCOMPLETE")
    thread.wait()
    window._worker_join_timeout_ms = 2000
    code, result = finish_qt_application(app, 0, max_wait_ms=2000)
    if code != 0 or not result.complete or shiboken6.isValid(window):
        print("FAIL resume after the worker finished", code, result.refused, file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("RESUME_OK")
    return 0


def main() -> int:
    mode = sys.argv[1]
    actions = {"refused": refused, "quit": external_quit, "bound": bounded_incomplete}
    return actions[mode]()


if __name__ == "__main__":
    raise SystemExit(main())
