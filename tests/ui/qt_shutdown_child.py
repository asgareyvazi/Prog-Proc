"""Subprocess body for the Qt shutdown contract. Not a pytest module.

The parent process owns the watchdog via ``subprocess`` timeout. This process
does not install a signal handler and cannot disarm that timeout. Modes named
``quit-active`` and ``deadline-synthetic`` use the ``Busy`` stand-in and are not
QThread integration. ``real-qthread``, ``quit-queued``, ``real-several``,
``finish-race`` and ``deadline`` own a real ``QThread``. A quit before
``exec()`` is recorded and is not a live-loop proof.

A quit issued before ``exec()`` does not stop the loop. The request is therefore
posted only from a ``QObject`` method that a ``QTimer`` delivers after ``exec()``
is already pumping events. A second timer, created inside that method, posts
``quit()`` or ``exit()`` as its own event so the call is not swallowed inside the
first timer slot. The application is ``create_application``: Qt would otherwise
ignore that quit when a window refuses ``close()``, and ``exec()`` would not
return. A daemon thread calls ``os._exit(6)`` if the loop never returns. That
is a failed proof. It does not call into Qt, and it does not disarm the parent
timeout.
"""

from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _drop_thread(thread: object) -> None:
    if thread.isRunning():  # type: ignore[attr-defined]
        thread.wait()  # type: ignore[attr-defined]
    # The exec-path stand-in is a plain object. ``isValid`` does not reject it
    # on the CI host, and ``delete`` then raises ``TypeError``.
    try:
        from PySide6.QtCore import QObject
    except (ImportError, OSError):
        return
    if not isinstance(thread, QObject):
        return
    try:
        import shiboken6
    except (ImportError, OSError):
        return
    try:
        valid = shiboken6.isValid(thread)
    except (TypeError, RuntimeError):
        return
    if valid:
        shiboken6.delete(thread)


def _sleeper(duration_ms: int) -> object:
    """Worker visible to ``closeEvent`` without a live ``QThread``.

    On the CI host, constructing a ``QThread`` and then entering ``exec()``
    stopped timer delivery, so quit never ran. ``closeEvent`` only calls
    ``isRunning``, ``requestInterruption``, ``quit`` and ``wait``. ``wait``
    honors the caller's timeout.
    """

    class Busy:
        def __init__(self, duration: int) -> None:
            self._until = time.monotonic() + duration / 1000

        def isRunning(self) -> bool:
            return time.monotonic() < self._until

        def requestInterruption(self) -> None:
            return None

        def quit(self) -> None:
            return None

        def start(self, priority: object = None) -> None:
            return None

        def wait(self, msecs: int = -1) -> bool:
            if not self.isRunning():
                return True
            if msecs == 0:
                return False
            limit = 30.0 if msecs < 0 else msecs / 1000
            deadline = min(self._until, time.monotonic() + limit)
            while self.isRunning() and time.monotonic() < deadline:
                time.sleep(0.01)
            return not self.isRunning()

    return Busy(duration_ms)


def _qt_sleeper(duration_ms: int) -> object:
    """Real ``QThread`` for the close tests that do not enter ``exec()``."""
    from PySide6.QtCore import QThread

    class Sleeper(QThread):
        def __init__(self, duration: int) -> None:
            super().__init__()
            self._duration_ms = duration

        def run(self) -> None:
            time.sleep(self._duration_ms / 1000)

    return Sleeper(duration_ms)


def _real_thread(duration_ms: int) -> object:
    """A genuine ``QThread``. Not the synthetic ``Busy`` stand-in.

    ``run`` sleeps so the test controls the lifetime without a worker that
    needs the GUI thread. ``start`` returns immediately. This object is what
    the external-quit integration and the production deadline proof own.
    """
    from PySide6.QtCore import QThread

    class Worker(QThread):
        def __init__(self, duration: int) -> None:
            super().__init__()
            self._duration_ms = duration

        def run(self) -> None:
            time.sleep(self._duration_ms / 1000)
            print("WORKER_FINISHED", flush=True)

    return Worker(duration_ms)


def _window(name: str, join_ms: int) -> object:
    from drilling_intelligence.ui.main_window import MainWindow

    window = MainWindow()
    window.setObjectName(name)
    window._worker_join_timeout_ms = join_ms
    return window


def _start_timer(owner: object, method: object, delay_ms: int) -> object:
    """Unparented timer, same connection the CI watchdog was able to deliver.

    The Python reference is kept by the caller. Parenting the timer, or using
    ``singleShot(0)`` before ``exec()``, did not stop the loop on this host.
    """
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(method)  # type: ignore[arg-type]
    timer.start(delay_ms)
    timers = getattr(owner, "_timers", None)
    if timers is None:
        owner._timers = []  # type: ignore[attr-defined]
        timers = owner._timers  # type: ignore[attr-defined]
    timers.append(timer)
    return timer


def _application(name: str) -> object:
    """Same object ``drillintel-ui`` constructs, so the quit proof is production."""
    from drilling_intelligence.ui.lifetime import create_application

    return create_application([name])


def _arm_exec_watchdog(owner: object) -> None:
    """Fail the proof if ``exec()`` is still running after 6s.

    A ``QTimer`` cannot fire while the GUI thread is stuck inside ``quit()``.
    This thread does not call Qt, so it can still ``os._exit(6)``. Exit 6 is
    a failed proof, not a pass, and it does not disarm the parent timeout.
    """

    def _abort() -> None:
        if not owner.exec_returned.wait(6):  # type: ignore[attr-defined]
            print("FAIL exec did not return within 6s", flush=True)
            os._exit(6)

    threading.Thread(target=_abort, daemon=True, name="qt-exec-watchdog").start()


def _guard_exec(probe: object) -> None:
    """Record that the loop never started. Do not call ``quit()`` from here.

    A non-GUI quit flushes the window-system queue and waits for the GUI
    thread. On this host that wait does not return while the first quit is
    stuck, so the child never reached ``os._exit``.
    """

    def run() -> None:
        if not probe.started.wait(5):  # type: ignore[attr-defined]
            print("FAIL loop did not start within 5s", flush=True)
            os._exit(6)

    threading.Thread(target=run, daemon=True).start()


def refused() -> int:
    import shiboken6
    from PySide6.QtWidgets import QWidget

    from drilling_intelligence.ui.lifetime import release_top_level_widgets

    app = _application("shutdown-refused")
    other = QWidget()
    other.setObjectName("other-window")
    other.show()
    window = _window("workbench", 40)
    thread = _qt_sleeper(250)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
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
    thread.wait()  # type: ignore[attr-defined]
    second = release_top_level_widgets(app)
    if not second.complete or shiboken6.isValid(window):
        print("FAIL cleanup after the worker finished", second.refused, file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("SHUTDOWN_OK")
    return 0


def _probe(application: object) -> object:
    from PySide6.QtCore import QObject

    class Probe(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.application = application
            self.window: object | None = None
            self.started = threading.Event()
            self.exec_returned = threading.Event()
            self._timers: list[object] = []

        def note_still_valid(self) -> None:
            import shiboken6

            window = self.window
            if window is not None and shiboken6.isValid(window):
                print("ABOUT_TO_QUIT_WINDOW_VALID", flush=True)
                return
            print("ABOUT_TO_QUIT_WINDOW_GONE", flush=True)

        def mark_loop_started(self) -> None:
            print("LOOP_STARTED", flush=True)
            self.started.set()

        def post_quit(self) -> None:
            print("QUIT_REQUESTED", flush=True)
            self.application.quit()  # type: ignore[attr-defined]

        def post_exit(self) -> None:
            print("EXIT_REQUESTED", flush=True)
            self.application.exit(7)  # type: ignore[attr-defined]

        def quit_once_running(self) -> None:
            self.mark_loop_started()
            _start_timer(self, self.post_quit, 30)

        def exit_once_running(self) -> None:
            self.mark_loop_started()
            _start_timer(self, self.post_exit, 30)

        def close_once_running(self) -> None:
            self.mark_loop_started()
            window = self.window
            accepted = window.close()  # type: ignore[attr-defined]
            print("CLOSE_REFUSED" if accepted is False else "CLOSE_ACCEPTED", flush=True)
            _start_timer(self, self.close_after_worker, 700)

        def close_after_worker(self) -> None:
            window = self.window
            accepted = window.close()  # type: ignore[attr-defined]
            label = "CLOSE_AGAIN_ACCEPTED" if accepted is not False else "CLOSE_AGAIN_REFUSED"
            print(label, flush=True)

    probe = Probe()
    application.aboutToQuit.connect(probe.note_still_valid)  # type: ignore[attr-defined]
    return probe


def _exec(application: object, probe: object) -> int:
    _arm_exec_watchdog(probe)
    code = int(application.exec())  # type: ignore[attr-defined]
    probe.exec_returned.set()  # type: ignore[attr-defined]
    return code


def _after_exec(application: object, window: object, code: int, *, deadline_ms: int) -> int:
    import shiboken6

    from drilling_intelligence.ui.lifetime import application_shutdown

    print("EXEC_RETURNED", flush=True)
    exit_code = application_shutdown(application, code, deadline_ms=deadline_ms)
    if exit_code != code or shiboken6.isValid(window):
        print("FAIL shutdown left a live window", exit_code, file=sys.stderr)
        return 1
    print(f"SHUTDOWN_CODE {exit_code}", flush=True)
    return 0


def quit_idle() -> int:
    app = _application("shutdown-quit-idle")
    window = _window("workbench", 40)
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=2_000) != 0:
        return 1
    print("IDLE_QUIT_OK")
    return 0


def quit_active() -> int:
    import shiboken6

    app = _application("shutdown-quit-active")
    window = _window("workbench", 40)
    thread = _sleeper(300)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    app.processEvents()
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if not shiboken6.isValid(window):
        print("FAIL exec destroyed the live window", file=sys.stderr)
        return 1
    if _after_exec(app, window, code, deadline_ms=4_000) != 0:
        return 1
    if thread.isRunning():  # type: ignore[attr-defined]
        print("FAIL worker still running after a successful shutdown", file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("EXTERNAL_QUIT_OK")
    return 0


def quit_several() -> int:
    app = _application("shutdown-quit-several")
    window = _window("workbench", 40)
    review = _sleeper(450)
    decision = _sleeper(150)
    window._review_thread = review  # type: ignore[attr-defined]
    window._decision_thread = decision  # type: ignore[attr-defined]
    review.start()  # type: ignore[attr-defined]
    decision.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=4_000) != 0:
        return 1
    if review.isRunning() or decision.isRunning():  # type: ignore[attr-defined]
        print("FAIL a worker was still running", file=sys.stderr)
        return 1
    _drop_thread(review)
    _drop_thread(decision)
    print("SEVERAL_OK")
    return 0


def last_window() -> int:
    app = _application("shutdown-last-window")
    app.setQuitOnLastWindowClosed(True)
    window = _window("workbench", 40)
    thread = _sleeper(400)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.close_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=2_000) != 0:
        return 1
    _drop_thread(thread)
    print("LAST_WINDOW_OK")
    return 0


def exit_route() -> int:
    app = _application("shutdown-exit")
    window = _window("workbench", 40)
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.exit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if code != 7:
        print("FAIL exit code was not preserved", code, file=sys.stderr)
        return 1
    if _after_exec(app, window, code, deadline_ms=2_000) != 0:
        return 1
    print("EXIT_ROUTE_OK")
    return 0


def production_deadline() -> int:
    """Production terminal with a short ceiling. A return is a failed test."""
    from drilling_intelligence.ui.lifetime import application_shutdown

    app = _application("shutdown-deadline")
    window = _window("workbench", 40)
    print("REAL_QTHREAD", flush=True)
    thread = _real_thread(30_000)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    print("EXEC_RETURNED", flush=True)
    application_shutdown(app, code, deadline_ms=400)
    print("FAIL deadline returned", file=sys.stderr)
    return 1


def bounded_incomplete() -> int:
    import shiboken6

    from drilling_intelligence.ui.lifetime import SHUTDOWN_INCOMPLETE_EXIT, finish_qt_application

    app = _application("shutdown-bound")
    window = _window("workbench", 30)
    thread = _qt_sleeper(5_000)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    code, result = finish_qt_application(app, 0, max_wait_ms=80)
    if code != SHUTDOWN_INCOMPLETE_EXIT or result.complete or not shiboken6.isValid(window):
        print("FAIL bounded wait declared success", code, result.refused, file=sys.stderr)
        return 1
    print("BOUNDED_INCOMPLETE")
    thread.wait()  # type: ignore[attr-defined]
    window._worker_join_timeout_ms = 2_000  # type: ignore[attr-defined]
    code, result = finish_qt_application(app, 0, max_wait_ms=2_000)
    if code != 0 or not result.complete or shiboken6.isValid(window):
        print("FAIL resume after the worker finished", code, result.refused, file=sys.stderr)
        return 1
    _drop_thread(thread)
    print("RESUME_OK")
    return 0


def real_qthread_quit() -> int:
    """External quit while a genuine QThread is running. Production cleanup."""
    import shiboken6

    app = _application("shutdown-real-qthread")
    window = _window("workbench", 40)
    thread = _real_thread(3_000)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    print("EXEC_RETURNED", flush=True)
    if not shiboken6.isValid(window):
        print("FAIL exec destroyed the live window", file=sys.stderr)
        return 1
    if thread.isRunning() is not True:  # type: ignore[attr-defined]
        print("FAIL worker finished before exec returned", file=sys.stderr)
        return 1
    from drilling_intelligence.ui.lifetime import application_shutdown

    exit_code = application_shutdown(app, code, deadline_ms=4_000)
    if exit_code != 0 or shiboken6.isValid(window) or thread.isRunning():  # type: ignore[attr-defined]
        print("FAIL real QThread shutdown", exit_code, file=sys.stderr)
        return 1
    print("SHUTDOWN_CODE 0", flush=True)
    print("REAL_QTHREAD_QUIT_OK", flush=True)
    return 0


def quit_queued() -> int:
    """A non-GUI thread queues quit. It does not call quit itself."""
    import shiboken6

    from drilling_intelligence.ui.lifetime import request_quit

    app = _application("shutdown-quit-queued")
    window = _window("workbench", 40)
    thread = _real_thread(500)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)

    def queued() -> None:
        if not probe.started.wait(5):  # type: ignore[attr-defined]
            print("FAIL loop did not start within 5s", flush=True)
            os._exit(6)
        print("QUIT_REQUESTED", flush=True)
        request_quit(app)

    threading.Thread(target=queued, daemon=True).start()
    held = _start_timer(probe, probe.mark_loop_started, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if not shiboken6.isValid(window):
        print("FAIL exec destroyed the live window", file=sys.stderr)
        return 1
    if _after_exec(app, window, code, deadline_ms=4_000) != 0:
        return 1
    print("QUEUED_QUIT_OK", flush=True)
    return 0


def quit_before_exec() -> int:
    """A quit issued before exec() must not be treated as a live-loop quit."""
    app = _application("shutdown-quit-before")
    window = _window("workbench", 40)
    window.show()  # type: ignore[attr-defined]
    app.quit()  # type: ignore[attr-defined]
    print("PRE_EXEC_QUIT", flush=True)
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=2_000) != 0:
        return 1
    # Either the pre-exec quit was ignored and the timer quit the live loop, or
    # exec() returned without pumping. Neither result is a live-loop proof.
    print("PRE_EXEC_NOT_A_LIVE_LOOP_PROOF", flush=True)
    return 0


def real_several() -> int:
    import shiboken6

    app = _application("shutdown-real-several")
    window = _window("workbench", 40)
    review = _real_thread(300)
    decision = _real_thread(500)
    window._review_thread = review  # type: ignore[attr-defined]
    window._decision_thread = decision  # type: ignore[attr-defined]
    review.start()  # type: ignore[attr-defined]
    decision.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if not shiboken6.isValid(window):
        print("FAIL exec destroyed the live window", file=sys.stderr)
        return 1
    if _after_exec(app, window, code, deadline_ms=4_000) != 0:
        return 1
    if review.isRunning() or decision.isRunning():  # type: ignore[attr-defined]
        print("FAIL a real worker was still running", file=sys.stderr)
        return 1
    _drop_thread(review)
    _drop_thread(decision)
    print("REAL_SEVERAL_OK", flush=True)
    return 0


def finish_race() -> int:
    """Quit while a real worker is about to finish. Either side of the race is success."""
    app = _application("shutdown-finish-race")
    window = _window("workbench", 40)
    thread = _real_thread(180)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=4_000) != 0:
        return 1
    print("RACE_OK", flush=True)
    return 0


def repeat_cleanup() -> int:
    from drilling_intelligence.ui.lifetime import application_shutdown

    app = _application("shutdown-repeat")
    window = _window("workbench", 40)
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    if _after_exec(app, window, code, deadline_ms=2_000) != 0:
        return 1
    again = application_shutdown(app, 0, deadline_ms=1_000)
    if again != 0:
        print("FAIL repeated cleanup", again, file=sys.stderr)
        return 1
    app.quit()  # type: ignore[attr-defined]
    print("AFTER_REPEAT_QUIT", flush=True)
    print("REPEAT_OK", flush=True)
    return 0


def pump_block() -> int:
    """A queued callback longer than processEvents' allowance must still exit 4."""
    from PySide6.QtCore import QTimer

    from drilling_intelligence.ui.lifetime import application_shutdown

    app = _application("shutdown-pump-block")
    window = _window("workbench", 40)
    window.show()  # type: ignore[attr-defined]

    def block() -> None:
        print("PUMP_BLOCKED", flush=True)
        time.sleep(30)

    QTimer.singleShot(0, block)
    print("PUMP_ARMED", flush=True)
    application_shutdown(app, 0, deadline_ms=300)
    print("FAIL pump block returned", file=sys.stderr)
    return 1


def deadline_synthetic() -> int:
    """Supplementary coordinator deadline. Not a QThread integration proof."""
    from drilling_intelligence.ui.lifetime import application_shutdown

    app = _application("shutdown-deadline-synthetic")
    window = _window("workbench", 40)
    thread = _sleeper(30_000)
    window._review_thread = thread  # type: ignore[attr-defined]
    thread.start()  # type: ignore[attr-defined]
    window.show()  # type: ignore[attr-defined]
    probe = _probe(app)
    probe.window = window  # type: ignore[attr-defined]
    _guard_exec(probe)
    held = _start_timer(probe, probe.quit_once_running, 200)  # type: ignore[attr-defined]
    code = _exec(app, probe)
    del held
    print("EXEC_RETURNED", flush=True)
    print("SYNTHETIC_DEADLINE", flush=True)
    application_shutdown(app, code, deadline_ms=400)
    print("FAIL deadline returned", file=sys.stderr)
    return 1


def main() -> int:
    actions = {
        "refused": refused,
        "quit-idle": quit_idle,
        "quit-active": quit_active,
        "quit-several": quit_several,
        "last-window": last_window,
        "exit-route": exit_route,
        "deadline": production_deadline,
        "deadline-synthetic": deadline_synthetic,
        "bound": bounded_incomplete,
        "real-qthread": real_qthread_quit,
        "quit-queued": quit_queued,
        "quit-before": quit_before_exec,
        "real-several": real_several,
        "finish-race": finish_race,
        "repeat": repeat_cleanup,
        "pump-block": pump_block,
    }
    return actions[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
