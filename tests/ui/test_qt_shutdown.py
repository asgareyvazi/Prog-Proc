"""Runtime shutdown contract. Each case is a fresh process so a native abort is visible.

The parent timeout is the watchdog. The child cannot disarm it: it does not
install a signal handler, and the timeout is not read from the child.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

_CHILD = Path(__file__).with_name("qt_shutdown_child.py")
_ABORTS = ("malloc_consolidate", "shared QObject was deleted directly")


def _qt_or_skip() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except (ImportError, OSError) as exc:
        pytest.skip(f"QtWidgets unavailable for UI tests: {exc}")


def _run(mode: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONUNBUFFERED"] = "1"
    try:
        return subprocess.run(
            [sys.executable, str(_CHILD), mode],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        steps = " | ".join(
            line.strip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()
        )
        pytest.fail(f"child timed out after {timeout}s :: {steps[-350:]}")


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    return " | ".join(
        line.strip()
        for line in f"{completed.stderr}\n{completed.stdout}".splitlines()
        if line.strip()
    )[-500:]


def _assert_no_abort(completed: subprocess.CompletedProcess[str]) -> None:
    blob = completed.stderr + completed.stdout
    for signature in _ABORTS:
        assert signature not in blob, _detail(completed)


def _assert_order(text: str, *markers: str) -> None:
    positions = []
    for marker in markers:
        assert marker in text, text[-500:]
        positions.append(text.index(marker))
    assert positions == sorted(positions), text[-500:]


def test_a_refused_close_keeps_the_window_and_a_later_pass_deletes_it() -> None:
    _qt_or_skip()
    completed = _run("refused", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    assert "REFUSED_KEPT_ALIVE" in completed.stdout
    assert "SHUTDOWN_OK" in completed.stdout
    _assert_no_abort(completed)


def test_external_quit_after_exec_starts_uses_application_shutdown() -> None:
    _qt_or_skip()
    idle = _run("quit-idle", timeout=20)
    assert idle.returncode == 0, _detail(idle)
    _assert_order(idle.stdout, "LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "IDLE_QUIT_OK")
    assert "ABOUT_TO_QUIT_WINDOW_VALID" in idle.stdout
    assert "SHUTDOWN_CODE 0" in idle.stdout
    _assert_no_abort(idle)

    active = _run("quit-active", timeout=20)
    assert active.returncode == 0, _detail(active)
    _assert_order(
        active.stdout,
        "LOOP_STARTED",
        "QUIT_REQUESTED",
        "ABOUT_TO_QUIT_WINDOW_VALID",
        "EXEC_RETURNED",
        "EXTERNAL_QUIT_OK",
    )
    assert "SHUTDOWN_CODE 0" in active.stdout
    _assert_no_abort(active)


def test_several_workers_and_last_window_and_exit_use_the_same_finish() -> None:
    _qt_or_skip()
    several = _run("quit-several", timeout=20)
    assert several.returncode == 0, _detail(several)
    _assert_order(several.stdout, "LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "SEVERAL_OK")
    _assert_no_abort(several)

    last = _run("last-window", timeout=20)
    assert last.returncode == 0, _detail(last)
    _assert_order(
        last.stdout,
        "LOOP_STARTED",
        "CLOSE_REFUSED",
        "CLOSE_AGAIN_ACCEPTED",
        "EXEC_RETURNED",
        "LAST_WINDOW_OK",
    )
    _assert_no_abort(last)

    exited = _run("exit-route", timeout=20)
    assert exited.returncode == 0, _detail(exited)
    _assert_order(
        exited.stdout,
        "LOOP_STARTED",
        "EXIT_REQUESTED",
        "EXEC_RETURNED",
        "SHUTDOWN_CODE 7",
        "EXIT_ROUTE_OK",
    )
    _assert_no_abort(exited)


def test_a_bounded_wait_reports_incomplete_until_the_worker_finishes() -> None:
    _qt_or_skip()
    completed = _run("bound", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    assert "BOUNDED_INCOMPLETE" in completed.stdout
    assert "RESUME_OK" in completed.stdout
    _assert_no_abort(completed)


def test_production_deadline_exits_4_and_the_parent_bounds_the_wait() -> None:
    _qt_or_skip()
    started = time.monotonic()
    completed = _run("deadline", timeout=8)
    elapsed = time.monotonic() - started
    assert completed.returncode == 4, _detail(completed)
    assert elapsed < 12, elapsed
    assert "Qt shutdown incomplete" in completed.stderr
    assert "FAIL deadline returned" not in completed.stderr + completed.stdout
    assert "REAL_QTHREAD" in completed.stdout
    _assert_order(completed.stdout, "LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED")
    _assert_no_abort(completed)


def test_synthetic_deadline_is_supplementary_not_the_qthread_proof() -> None:
    _qt_or_skip()
    started = time.monotonic()
    completed = _run("deadline-synthetic", timeout=8)
    elapsed = time.monotonic() - started
    assert completed.returncode == 4, _detail(completed)
    assert elapsed < 6, elapsed
    assert "SYNTHETIC_DEADLINE" in completed.stdout
    assert "Qt shutdown incomplete" in completed.stderr
    _assert_no_abort(completed)


def test_real_qthread_external_quit_uses_application_shutdown() -> None:
    """Integration proof. The Busy stand-in in quit-active is not this test."""
    _qt_or_skip()
    completed = _run("real-qthread", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    _assert_order(
        completed.stdout,
        "LOOP_STARTED",
        "QUIT_REQUESTED",
        "ABOUT_TO_QUIT_WINDOW_VALID",
        "EXEC_RETURNED",
        "WORKER_FINISHED",
        "SHUTDOWN_CODE 0",
        "REAL_QTHREAD_QUIT_OK",
    )
    _assert_no_abort(completed)


def test_queued_quit_from_another_thread_runs_on_the_gui_thread() -> None:
    _qt_or_skip()
    completed = _run("quit-queued", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    _assert_order(
        completed.stdout,
        "LOOP_STARTED",
        "QUIT_REQUESTED",
        "ABOUT_TO_QUIT_WINDOW_VALID",
        "EXEC_RETURNED",
        "QUEUED_QUIT_OK",
    )
    _assert_no_abort(completed)


def test_quit_before_exec_is_not_a_live_loop_quit() -> None:
    _qt_or_skip()
    completed = _run("quit-before", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    assert "PRE_EXEC_QUIT" in completed.stdout
    assert "PRE_EXEC_NOT_A_LIVE_LOOP_PROOF" in completed.stdout
    assert "REAL_QTHREAD_QUIT_OK" not in completed.stdout
    if "LOOP_STARTED" in completed.stdout:
        _assert_order(
            completed.stdout,
            "PRE_EXEC_QUIT",
            "LOOP_STARTED",
            "QUIT_REQUESTED",
            "EXEC_RETURNED",
        )
    else:
        assert "QUIT_REQUESTED" not in completed.stdout
        assert "EXEC_RETURNED" in completed.stdout
    _assert_no_abort(completed)


def test_several_real_qthreads_and_a_finish_race_use_production_shutdown() -> None:
    _qt_or_skip()
    several = _run("real-several", timeout=20)
    assert several.returncode == 0, _detail(several)
    _assert_order(
        several.stdout, "LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "REAL_SEVERAL_OK"
    )
    _assert_no_abort(several)
    raced = _run("finish-race", timeout=20)
    assert raced.returncode == 0, _detail(raced)
    assert "RACE_OK" in raced.stdout
    _assert_no_abort(raced)


def test_repeated_cleanup_and_a_quit_after_exec_do_not_hang() -> None:
    _qt_or_skip()
    completed = _run("repeat", timeout=20)
    assert completed.returncode == 0, _detail(completed)
    _assert_order(
        completed.stdout, "EXEC_RETURNED", "SHUTDOWN_CODE 0", "AFTER_REPEAT_QUIT", "REPEAT_OK"
    )
    _assert_no_abort(completed)


def test_a_blocking_callback_cannot_defeat_the_shutdown_deadline() -> None:
    _qt_or_skip()
    started = time.monotonic()
    completed = _run("pump-block", timeout=8)
    elapsed = time.monotonic() - started
    assert completed.returncode == 4, _detail(completed)
    assert elapsed < 3, elapsed
    assert "deadline elapsed" in completed.stderr
    assert "FAIL pump block returned" not in completed.stderr + completed.stdout
    assert "PUMP_ARMED" in completed.stdout
    assert "PUMP_BLOCKED" in completed.stdout
    _assert_no_abort(completed)
