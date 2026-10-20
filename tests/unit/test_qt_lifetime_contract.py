"""The workbench must not leave Qt objects for interpreter shutdown."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _tree(relative: str) -> ast.AST:
    return ast.parse(Path(relative).read_text(encoding="utf-8"), filename=relative)


def test_release_result_is_validity_not_a_count() -> None:
    from drilling_intelligence.ui.lifetime import ReleaseResult

    assert ReleaseResult(3, ()).complete is True
    refused = ReleaseResult(3, ("workbench",))
    assert refused.complete is False
    assert refused.released == 3


def test_close_waits_for_every_worker_thread_attribute() -> None:
    tree = _tree("src/drilling_intelligence/ui/main_window.py")
    constant = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_WORKER_THREAD_ATTRS":
                constant = node
    assert constant is not None
    declared = {elt.value for elt in constant.value.elts if isinstance(elt, ast.Constant)}
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr.endswith("_thread"):
                assigned.add(target.attr)
    assert assigned <= declared
    assert "_decision_thread" in declared
    assert "_comparison_thread" in declared
    source = Path("src/drilling_intelligence/ui/main_window.py").read_text(encoding="utf-8")
    assert "lambda:" not in source


def test_application_and_fixture_use_the_same_shutdown_contract() -> None:
    app = Path("src/drilling_intelligence/ui/app.py").read_text(encoding="utf-8")
    fixture = Path("tests/ui/test_workbench.py").read_text(encoding="utf-8")
    lifetime = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    assert "application_shutdown" in app
    assert "create_application" in app
    assert "QApplication(" not in app
    assert "os._exit" not in app
    shutdown = lifetime.split("def application_shutdown", 1)[1].split("def fixture_teardown", 1)[0]
    assert "finish_qt_application" in shutdown
    assert "fail_closed_shutdown" in shutdown
    assert "deadline_ms=deadline_ms" in shutdown
    assert "fixture_teardown" in fixture
    assert "application.quit()" not in fixture.split("yield application", 1)[1]
    assert "shiboken6.delete" in lifetime
    assert "widget.close() is not False" in lifetime
    assert "shiboken6.isValid" in lifetime
    assert "PRODUCTION_SHUTDOWN_WAIT_MS = 120_000" in lifetime
    coordinate = lifetime.split("def _coordinate", 1)[1].split("def finish_qt_application", 1)[0]
    assert ".exec()" not in coordinate
    assert "QEventLoop()" not in coordinate
    assert "elapsed.start()" not in coordinate
    assert coordinate.count("_ShutdownDeadline(") == 1
    assert "if result.released:" in coordinate
    assert "if not guard.disarm()" in coordinate
    assert "PRODUCTION_SHUTDOWN_WAIT_MS" not in coordinate
    teardown = lifetime.split("def fixture_teardown", 1)[1]
    assert teardown.index("os._exit") < teardown.index("application.quit()")
    window = Path("src/drilling_intelligence/ui/main_window.py").read_text(encoding="utf-8")
    assert "self._worker_join_timeout_ms = 10_000" in window
    assert "_shutdown_join_budget_ms" in window
    assert "thread.wait(limit)" in window
    assert "def owned_workers_running" in window
    assert "owned_workers_running" in lifetime
    accept = lifetime.split("def create_application", 1)[1].split("def application_shutdown", 1)[0]
    assert "QCoreApplication.exit(0)" in accept
    assert "installEventFilter" in accept
    assert "closeAllWindows" not in accept
    assert "thread.wait" not in accept
    assert "currentThread() is " not in accept
    assert "getCppPointer" in accept
    assert "_ShutdownDeadline" in coordinate
    assert "disarm" in coordinate


def test_shutdown_child_cannot_disarm_the_parent_watchdog() -> None:
    child = Path("tests/ui/qt_shutdown_child.py").read_text(encoding="utf-8")
    parent = Path("tests/ui/test_qt_shutdown.py").read_text(encoding="utf-8")
    assert "SIG_IGN" not in child
    assert "signal.signal" not in child
    assert "application_shutdown" in child
    assert "create_application" in child
    assert "QApplication(" not in child
    assert "QCoreApplication.quit()" not in child
    deadline = child.split("def production_deadline", 1)[1].split("def bounded_incomplete", 1)[0]
    assert "deadline_ms=400" in deadline
    assert "max_wait_ms" not in deadline
    assert "os._exit" not in deadline
    active = child.split("def quit_active", 1)[1].split("def quit_several", 1)[0]
    assert active.index("_exec(app, probe)") < active.index("_after_exec")
    assert "quit()" not in active
    sleeper = child.split("def _sleeper", 1)[1].split("def _window", 1)[0]
    assert "time.sleep" in sleeper
    assert "timeout=" in parent
    assert "timeout=8" in parent
    assert "class Worker(QThread)" in child
    assert "real-qthread" in child
    assert "REAL_QTHREAD_QUIT_OK" in child
    assert "request_quit" in child
    assert "QCoreApplication.quit()" not in child
    deadline_body = child.split("def production_deadline", 1)[1].split("def bounded_incomplete", 1)[
        0
    ]
    assert "_real_thread" in deadline_body
    assert "_sleeper" not in deadline_body


def test_one_shutdown_budget_is_not_restarted_and_does_not_shrink_a_covered_join() -> None:
    from drilling_intelligence.ui.lifetime import (
        remaining_budget_ms,
        wait_slice_ms,
        worker_join_limit_ms,
    )

    assert remaining_budget_ms(120_000, 10_000) == 110_000
    assert remaining_budget_ms(250, 250) == 0
    assert remaining_budget_ms(250, 300) < 0
    # The production 10s join is unchanged while the 120s ceiling still covers it.
    assert worker_join_limit_ms(10_000, 120_000) == 10_000
    assert worker_join_limit_ms(10_000, 50) == 50
    assert worker_join_limit_ms(40, 0) == 0
    assert wait_slice_ms(5) == 5
    assert wait_slice_ms(0) == 0


def test_deadline_guard_exits_4_and_a_disarmed_guard_does_not() -> None:
    import subprocess
    import sys
    import time

    from drilling_intelligence.ui.lifetime import _ShutdownDeadline

    guard = _ShutdownDeadline(80)
    guard.disarm()
    time.sleep(0.2)
    script = (
        "from drilling_intelligence.ui.lifetime import _ShutdownDeadline\n"
        "import time\n"
        "_ShutdownDeadline(150)\n"
        "time.sleep(3)\n"
        "raise SystemExit(0)\n"
    )
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 4, completed.stderr
    assert elapsed < 1.5, elapsed
    assert "deadline elapsed" in completed.stderr
    assert "Traceback" not in completed.stderr


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_completion_is_accepted_only_strictly_before_the_deadline() -> None:
    from drilling_intelligence.ui.lifetime import _ShutdownDeadline

    clock = _Clock(10.0)
    before = _ShutdownDeadline(1_000, clock=clock, armed=False)
    assert before.disarm() is True
    assert before.disarm() is True
    assert before.enforce() is False
    at_deadline = _ShutdownDeadline(0, clock=clock, armed=False)
    assert at_deadline.disarm() is False
    assert at_deadline.disarm() is False
    after = _ShutdownDeadline(500, clock=_Clock(10.0), armed=False)
    after_clock = after._clock
    assert after_clock() < after._deadline
    after._clock = _Clock(after._deadline + 0.001)
    assert after.disarm() is False


def test_windows_access_violation_is_not_fail_closed_exit_4() -> None:
    from drilling_intelligence.ui.lifetime import is_fail_closed_shutdown_exit

    assert is_fail_closed_shutdown_exit(4)
    assert not is_fail_closed_shutdown_exit(0)
    assert not is_fail_closed_shutdown_exit(1)
    assert not is_fail_closed_shutdown_exit(134)
    assert not is_fail_closed_shutdown_exit(True)  # type: ignore[arg-type]
    assert not is_fail_closed_shutdown_exit(3221225477)
    assert not is_fail_closed_shutdown_exit(-1073741819)
    assert 3221225477 == 0xC0000005
    assert (-1073741819) % (1 << 32) == 0xC0000005


def test_windows_deadline_monitor_exits_without_notifying_libraries(monkeypatch) -> None:
    import drilling_intelligence.ui.lifetime as lifetime

    calls: list[tuple[str, int]] = []
    # A fresh arbiter per call: the terminal exit is claimed once per process,
    # and this test drives the two platform variants one after the other.
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_write_deadline_notice", lambda *_args: None)
    monkeypatch.setattr(
        lifetime,
        "_terminate_current_process",
        lambda code: calls.append(("terminate", code)),
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(("exit", code)))
    lifetime._terminal_exit()
    assert calls == [("terminate", 4), ("exit", 4)]
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: False)
    lifetime._terminal_exit()
    assert calls == [("terminate", 4), ("exit", 4), ("exit", 4)]
    source = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    assert "QThread.terminate(" not in source
    assert "TerminateProcess" in source
    assert (
        "shiboken6.delete"
        not in source.split("def _terminate_current_process", 1)[1].split("def _terminal_exit", 1)[
            0
        ]
    )


def test_the_coordinator_also_ends_the_process_without_notifying_libraries(monkeypatch) -> None:
    """``os._exit`` on Windows notifies the libraries; the contract forbids that.

    ``ExitProcess`` runs the Python finalizer while the ``QApplication`` and
    a live ``QThread`` still exist. Destroying them there faulted the process
    with ``0xC0000005`` instead of the contracted exit 4.
    """
    import drilling_intelligence.ui.lifetime as lifetime

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(
        lifetime,
        "_terminate_current_process",
        lambda code: calls.append(("terminate", code)),
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(("exit", code)))
    lifetime.fail_closed_shutdown(lifetime.ReleaseResult(0, ("workbench",)))
    assert calls == [("terminate", 4), ("exit", 4)]
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: False)
    lifetime.fail_closed_shutdown(lifetime.ReleaseResult(0, ("workbench",)))
    assert calls == [("terminate", 4), ("exit", 4), ("exit", 4)]
    source = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    for name in ("def fail_closed_shutdown", "def _terminal_exit"):
        body = source.split(name, 1)[1].split("\n\ndef ", 1)[0]
        assert "os._exit" not in body, name
        assert "_end_process(" in body, name


def test_only_one_thread_may_end_the_process() -> None:
    from drilling_intelligence.ui.lifetime import _TerminalExit

    arbiter = _TerminalExit()
    assert arbiter.claim("coordinator") is True
    assert arbiter.claim("monitor") is False
    assert arbiter.owner() == "coordinator"


def test_a_thread_that_loses_the_terminal_race_stands_down(monkeypatch) -> None:
    import drilling_intelligence.ui.lifetime as lifetime

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_end_process", lambda code: calls.append(("end", code)))
    monkeypatch.setattr(
        lifetime,
        "_stand_down_for_terminal_exit",
        lambda: calls.append(("standdown", 0)),
    )
    assert lifetime._TERMINAL_EXIT.claim("monitor") is True
    lifetime.fail_closed_shutdown(lifetime.ReleaseResult(0, ("workbench",)))
    assert calls == [("standdown", 0)]
    lifetime._terminal_exit()
    assert calls == [("standdown", 0), ("standdown", 0)]


def test_the_coordinator_and_the_monitor_cannot_both_end_the_process(monkeypatch) -> None:
    """The race that produced the Windows access violation.

    The coordinator polls the release budget and the monitor guarantees the
    ceiling. Both can observe the same expiry. Exactly one of them may end
    the process.
    """
    import threading

    import drilling_intelligence.ui.lifetime as lifetime

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_write_deadline_notice", lambda *_args: None)
    monkeypatch.setattr(lifetime, "_end_process", lambda code: calls.append(("end", code)))
    monkeypatch.setattr(
        lifetime,
        "_stand_down_for_terminal_exit",
        lambda: calls.append(("standdown", 0)),
    )
    barrier = threading.Barrier(2)

    def coordinator() -> None:
        barrier.wait()
        lifetime.fail_closed_shutdown(lifetime.ReleaseResult(0, ("workbench",)))

    def monitor() -> None:
        barrier.wait()
        lifetime._terminal_exit()

    threads = [threading.Thread(target=coordinator), threading.Thread(target=monitor)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [call for call in calls if call[0] == "end"] == [("end", 4)]
    assert len([call for call in calls if call[0] == "standdown"]) == 1


def test_a_late_disarm_cannot_suppress_the_terminal_exit() -> None:
    from drilling_intelligence.ui.lifetime import _ShutdownDeadline

    exits: list[int] = []
    clock = _Clock(5.0)
    guard = _ShutdownDeadline(0, clock=clock, terminal=lambda: exits.append(4), armed=False)
    assert guard.disarm() is False
    assert guard.enforce() is True
    assert exits == [4]
    assert guard.enforce() is False
    assert guard.disarm() is False


def test_a_completion_and_expiry_race_cannot_report_success() -> None:
    import threading

    from drilling_intelligence.ui.lifetime import _ShutdownDeadline

    clock = _Clock(8.0)
    guard = _ShutdownDeadline(0, clock=clock, terminal=lambda: None, armed=False)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def complete() -> None:
        barrier.wait()
        results["complete"] = guard.disarm()

    def expire() -> None:
        barrier.wait()
        results["expire"] = guard.enforce()

    threads = [threading.Thread(target=complete), threading.Thread(target=expire)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["complete"] is False


def test_zero_deadline_exits_4_without_waiting_out_the_parent() -> None:
    import subprocess
    import sys
    import time

    script = (
        "from drilling_intelligence.ui.lifetime import _ShutdownDeadline\n"
        "import time\n"
        "_ShutdownDeadline(0)\n"
        "time.sleep(3)\n"
        "raise SystemExit(0)\n"
    )
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 4, completed.stderr
    assert time.monotonic() - started < 1.5


def test_a_release_exception_still_fails_closed(monkeypatch) -> None:
    from drilling_intelligence.ui import lifetime

    def _raise(*_args, **_kwargs):
        raise RuntimeError("wrapped C/C++ object has been deleted")

    seen: list[lifetime.ReleaseResult] = []

    def _stop(result: lifetime.ReleaseResult) -> None:
        seen.append(result)
        raise SystemExit(lifetime.SHUTDOWN_INCOMPLETE_EXIT)

    monkeypatch.setattr(lifetime, "finish_qt_application", _raise)
    monkeypatch.setattr(lifetime, "fail_closed_shutdown", _stop)
    with pytest.raises(SystemExit) as caught:
        lifetime.application_shutdown(object(), 0, deadline_ms=400)
    assert caught.value.code == lifetime.SHUTDOWN_INCOMPLETE_EXIT
    assert seen[0].refused == ("release-exception",)


def test_lifetime_does_not_import_fcntl_at_module_scope() -> None:
    source = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "fcntl" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "fcntl"


def test_posix_deadline_stderr_is_nonblocking() -> None:
    import os

    from drilling_intelligence.ui.lifetime import _set_nonblocking

    read_fd, write_fd = os.pipe()
    try:
        if os.name == "nt":
            with pytest.raises(ModuleNotFoundError):
                _set_nonblocking(write_fd)
            return
        import fcntl

        _set_nonblocking(write_fd)
        flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
        assert flags & os.O_NONBLOCK
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_windows_deadline_notice_does_not_wait_on_a_full_pipe(monkeypatch) -> None:
    import os
    import threading
    import time

    monkeypatch.setattr("drilling_intelligence.ui.lifetime.os.name", "nt")
    from drilling_intelligence.ui.lifetime import _write_deadline_notice

    read_fd, write_fd = os.pipe()
    filler = threading.Thread(
        target=lambda: _fill_until_blocked(write_fd),
        name="pipe-fill",
        daemon=True,
    )
    try:
        filler.start()
        time.sleep(0.2)
        started = time.monotonic()
        _write_deadline_notice(write_fd, b"notice\n")
        assert time.monotonic() - started < 1.0
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _fill_until_blocked(fd: int) -> None:
    import os

    chunk = b"x" * 4096
    try:
        while True:
            os.write(fd, chunk)
    except (BlockingIOError, OSError):
        return
