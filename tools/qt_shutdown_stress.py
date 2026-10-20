#!/usr/bin/env python3
"""Repeat the fail-closed shutdown children and record every attempt.

This is an experiment, not a retry. Every attempt is written to disk and
reported: a later attempt that honours the contract does not erase an
earlier native fault, and the tool fails when *any* attempt ends with
something other than the contracted fail-closed exit.

The contracted exit is the integer ``4`` (``SHUTDOWN_INCOMPLETE_EXIT``). The
Windows access violation that this repository has seen is reported as
``3221225477`` (``0xC0000005``); that is a fault, not a pass, and it is
reported as such. A negative code is a signal on POSIX.

The child is the same process the UI test suite starts. It is started in its
own interpreter so that a native abort is visible as a process exit code
instead of taking the test runner with it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from drilling_intelligence.ui.lifetime import (
    SHUTDOWN_INCOMPLETE_EXIT,
    is_fail_closed_shutdown_exit,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "ui" / "qt_shutdown_child.py"
DEFAULT_MODES = ("deadline", "deadline-synthetic")
WINDOWS_ACCESS_VIOLATION = 3221225477


def contract_failure(code: int) -> str:
    """Why an exit code is not the contracted fail-closed exit.

    An empty string means the attempt honoured the contract. The wording is
    a category, not a document body: it never contains the child's output.
    """
    if is_fail_closed_shutdown_exit(code):
        return ""
    if code == WINDOWS_ACCESS_VIOLATION:
        return "windows access violation 0xC0000005"
    if code < 0:
        return f"killed by signal {-code}"
    return f"exit {code} is not the contracted {SHUTDOWN_INCOMPLETE_EXIT}"


def runtime_versions() -> dict[str, str]:
    """The runtime that produced the evidence. A missing toolkit is recorded."""
    versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    try:
        import PySide6
        from PySide6.QtCore import qVersion
    except (ImportError, OSError, RuntimeError):
        versions["pyside"] = "unavailable"
        versions["qt"] = "unavailable"
        return versions
    versions["pyside"] = str(PySide6.__version__)
    versions["qt"] = str(qVersion())
    return versions


def run_attempt(mode: str, *, timeout: float) -> tuple[int, str, float]:
    """Run one child. Returns the exit code, the combined output, and the duration."""
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(CHILD), mode],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        body = f"{exc.stdout or ''}\n{exc.stderr or ''}"
        return -1, f"child timed out after {timeout}s\n{body}", time.monotonic() - started
    body = completed.stdout + completed.stderr
    return completed.returncode, body, time.monotonic() - started


def _terminal_markers(body: str) -> list[str]:
    """The shutdown transcript lines that identify who ended the process."""
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("SHUTDOWN t=") or "TERMINAL_ATTEMPT" in line
    ]


def stress(
    modes: tuple[str, ...],
    *,
    attempts: int,
    timeout: float,
    log_dir: Path,
    runner: Callable[[str, float], tuple[int, str, float]] | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """Run every attempt and report every one. The first failure is not a retry."""
    log_dir.mkdir(parents=True, exist_ok=True)
    run = runner or (lambda mode, limit: run_attempt(mode, timeout=limit))
    records: list[dict[str, object]] = []
    failures = 0
    for mode in modes:
        for ordinal in range(1, attempts + 1):
            code, body, duration = run(mode, timeout)
            reason = contract_failure(code)
            if reason:
                failures += 1
            name = f"{mode}-attempt-{ordinal:03d}.txt"
            (log_dir / name).write_text(
                f"mode={mode} ordinal={ordinal} exit={code} "
                f"duration_s={duration:.3f} reason={reason or '-'}\n{body}",
                encoding="utf-8",
            )
            record: dict[str, object] = {
                "mode": mode,
                "ordinal": ordinal,
                "exit": code,
                "duration_s": round(duration, 3),
                "reason": reason,
                "log": name,
                "markers": _terminal_markers(body),
            }
            records.append(record)
            print(
                f"::notice::shutdown-stress mode={mode} ordinal={ordinal} "
                f"exit={code} reason={reason or '-'}",
            )
            print(
                f"mode={mode} ordinal={ordinal} exit={code} "
                f"duration_s={duration:.3f} reason={reason or '-'}",
                flush=True,
            )
    return failures, records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--mode", action="append", default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("qt-logs") / "shutdown-stress",
    )
    args = parser.parse_args(argv)
    if args.attempts < 1:
        print("shutdown stress attempts must be at least 1", file=sys.stderr)
        return 2
    modes = tuple(args.mode) if args.mode else DEFAULT_MODES
    if not CHILD.is_file():
        print(f"shutdown child is missing: {CHILD}", file=sys.stderr)
        return 2
    failures, records = stress(
        modes,
        attempts=args.attempts,
        timeout=args.timeout,
        log_dir=args.log_dir,
    )
    total = len(records)
    summary = {
        "attempts": total,
        "modes": list(modes),
        "per_mode_attempts": args.attempts,
        "failures": failures,
        "contracted_exit": SHUTDOWN_INCOMPLETE_EXIT,
        "versions": runtime_versions(),
        "records": records,
    }
    args.log_dir.mkdir(parents=True, exist_ok=True)
    (args.log_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"shutdown stress: attempts={total} failures={failures} "
        f"contracted_exit={SHUTDOWN_INCOMPLETE_EXIT}",
        flush=True,
    )
    for record in records:
        if record["reason"]:
            print(
                f"::error::shutdown-stress {record['mode']} #{record['ordinal']}: {record['reason']}"
            )
    if failures:
        print(
            f"shutdown stress FAILED: {failures} of {total} attempts did not honour "
            f"exit {SHUTDOWN_INCOMPLETE_EXIT}",
            file=sys.stderr,
        )
        return 1
    print("shutdown stress PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
