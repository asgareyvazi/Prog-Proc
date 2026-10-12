"""Runtime shutdown contract. Each case is a fresh process so a native abort is visible."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

_CHILD = Path(__file__).with_name("qt_shutdown_child.py")


def _qt_or_skip() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except (ImportError, OSError) as exc:
        pytest.skip(f"QtWidgets unavailable for UI tests: {exc}")


def _run(mode: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [sys.executable, str(_CHILD), mode],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_a_refused_close_keeps_the_window_and_a_later_pass_deletes_it() -> None:
    _qt_or_skip()
    completed = _run("refused")
    assert completed.returncode == 0, completed.stderr
    assert "REFUSED_KEPT_ALIVE" in completed.stdout, completed.stdout + completed.stderr
    assert "SHUTDOWN_OK" in completed.stdout, completed.stdout + completed.stderr
    assert "malloc_consolidate" not in completed.stderr
    assert "shared QObject was deleted directly" not in completed.stderr


def test_external_quit_does_not_bypass_widget_release() -> None:
    _qt_or_skip()
    completed = _run("quit")
    detail = " | ".join(
        line.strip()
        for line in f"{completed.stderr}\n{completed.stdout}".splitlines()
        if line.strip()
    )[-400:]
    if completed.returncode != 0 or "EXTERNAL_QUIT_OK" not in completed.stdout:
        print(f"::error::external quit shutdown failed: {detail}")
    assert completed.returncode == 0, detail
    assert "EXTERNAL_QUIT_OK" in completed.stdout, detail
    assert "QUIT_KEPT_ALIVE" in completed.stdout, detail


def test_a_bounded_wait_reports_incomplete_until_the_worker_finishes() -> None:
    _qt_or_skip()
    completed = _run("bound")
    assert completed.returncode == 0, completed.stderr
    assert "BOUNDED_INCOMPLETE" in completed.stdout, completed.stdout + completed.stderr
    assert "RESUME_OK" in completed.stdout, completed.stdout + completed.stderr
