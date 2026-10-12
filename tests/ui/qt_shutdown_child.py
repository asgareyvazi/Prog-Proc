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
    import shiboken6
    from PySide6.QtCore import QObject, QThread, QTimer
    from PySide6.QtWidgets import QApplication

    from drilling_intelligence.ui.lifetime import release_top_level_widgets
    from drilling_intelligence.ui.main_window import MainWindow

    class Sleeper(QThread):
        def run(self) -> None:
            self.msleep(200)

    class Quitter(QObject):
        def __init__(self, application: QApplication) -> None:
            super().__init__()
            self._application = application

        def quit_app(self) -> None:
            self._application.quit()

        def abort_stuck_exec(self) -> None:
            print("FAIL exec did not return", file=sys.stderr)
            os._exit(5)

    app = QApplication(["shutdown-quit"])
    window = MainWindow()
    window.setObjectName("workbench")
    window._worker_join_timeout_ms = 40
    thread = Sleeper()
    window._review_thread = thread
    thread.start()
    window.show()
    quitter = Quitter(app)
    # A nested Python callable was not invoked, so exec() never returned.
    # A QObject method is a real slot. The watchdog labels a stuck loop.
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(quitter.abort_stuck_exec)
    watchdog.start(3000)
    QTimer.singleShot(0, quitter.quit_app)
    app.exec()
    watchdog.stop()
    if not shiboken6.isValid(window):
        print("FAIL quit destroyed the live window", file=sys.stderr)
        return 1
    # One close, not a loop. Repeated close() while quit is sticky can keep
    # the worker from ever leaving isRunning().
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
