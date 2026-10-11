#!/usr/bin/env python3
"""Publish explicit pytest totals.

The CI log uses one quiet level, which still prints a summary when the process
finishes. This record is what remains when that line is missing or the suite
failed: collected, passed, failed, errors, skipped, xfailed, xpassed, runtime,
and the process exit code. JUnit does not mark an xpass, so the session stats
are the authority and JUnit is the cross-check. A green workflow cannot omit
either file, and a disagreement fails the job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_XFAIL_TYPE = "pytest.xfail"
_XPASS_MESSAGE = "xfail-marked test passes unexpectedly"
_KEYS = (
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "exit",
    "runtime",
)


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _suites(root: ET.Element) -> list[ET.Element]:
    if root.tag == "testsuite":
        return [root]
    return list(root.findall("testsuite"))


def totals_from_junit(path: Path, exit_code: int | None = None) -> dict[str, int | float | None]:
    """Count outcomes. xfail is split out of the skipped bucket.

    An unexpected pass is a normal ``testcase`` in JUnit, so ``xpassed`` here
    stays 0. ``totals_from_stats`` is the authority for that count.
    """
    # The file is pytest's own JUnit artifact, not an external document.
    root = ET.parse(path).getroot()  # noqa: S314
    suites = _suites(root)
    if not suites:
        raise ValueError(f"{path} has no testsuite element")
    collected = failures = errors = skipped_attr = 0
    runtime = 0.0
    xfailed = xpassed = 0
    for suite in suites:
        collected += _int(suite.attrib.get("tests"))
        failures += _int(suite.attrib.get("failures"))
        errors += _int(suite.attrib.get("errors"))
        skipped_attr += _int(suite.attrib.get("skipped"))
        runtime += float(suite.attrib.get("time") or 0)
        for skipped in suite.iter("skipped"):
            kind = skipped.attrib.get("type") or ""
            message = skipped.attrib.get("message") or ""
            if kind == _XFAIL_TYPE:
                xfailed += 1
            elif message.startswith(_XPASS_MESSAGE):
                xpassed += 1
    skipped = skipped_attr - xfailed - xpassed
    passed = collected - failures - errors - skipped_attr
    return {
        "collected": collected,
        "passed": passed,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "exit": exit_code,
        "runtime": round(runtime, 3),
    }


def totals_from_stats(
    stats: dict[str, list[object]],
    *,
    collected: int,
    exit_code: int | None,
    runtime: float,
) -> dict[str, int | float | None]:
    def count(key: str) -> int:
        return len(stats.get(key, []))

    return {
        "collected": collected,
        "passed": count("passed"),
        "failed": count("failed"),
        "errors": count("error"),
        "skipped": count("skipped"),
        "xfailed": count("xfailed"),
        "xpassed": count("xpassed"),
        "exit": exit_code,
        "runtime": round(runtime, 3),
    }


def disagreements(
    recorded: dict[str, int | float | None], junit: dict[str, int | float | None]
) -> list[str]:
    """Return cross-check failures. JUnit folds xpass into passed."""
    problems: list[str] = []
    for key in ("collected", "failed", "errors", "skipped", "xfailed"):
        if _int(recorded.get(key)) != _int(junit.get(key)):
            problems.append(f"{key}: stats {recorded.get(key)} != junit {junit.get(key)}")
    recorded_passed = _int(recorded.get("passed")) + _int(recorded.get("xpassed"))
    junit_passed = _int(junit.get("passed")) + _int(junit.get("xpassed"))
    if recorded_passed != junit_passed:
        problems.append(f"passed+xpassed: stats {recorded_passed} != junit {junit_passed}")
    return problems


def format_totals(totals: dict[str, int | float | None]) -> str:
    lines = ["pytest totals"]
    for key in _KEYS:
        lines.append(f"{key}: {totals.get(key)}")
    return "\n".join(lines)


def _read_exit(path: Path | None) -> int | None:
    if path is None or not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if raw == "":
        return None
    return int(raw)


def _write_summary(summary_path: Path | None, text: str) -> None:
    print(text)
    if summary_path is None:
        return
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write("### Pytest totals\n\n```\n" + text + "\n```\n")


def _load_stats(path: Path, exit_code: int | None) -> dict[str, int | float | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    totals = {key: payload[key] for key in _KEYS if key != "exit"}
    totals["exit"] = exit_code if exit_code is not None else payload.get("exit")
    return totals


def publish(
    stats_path: Path,
    junit_path: Path,
    exit_file: Path | None,
    summary_path: Path | None,
) -> int:
    """Print totals and fail unless both records exist, agree, and the suite passed."""
    exit_code = _read_exit(exit_file)
    if not stats_path.is_file() or not junit_path.is_file():
        text = "\n".join(
            [
                "pytest totals",
                f"stats: {'present' if stats_path.is_file() else 'missing'}",
                f"junit: {'present' if junit_path.is_file() else 'missing'}",
                f"exit: {exit_code if exit_code is not None else 'unknown'}",
            ]
        )
        _write_summary(summary_path, text)
        return 1
    recorded = _load_stats(stats_path, exit_code)
    junit = totals_from_junit(junit_path, exit_code)
    problems = disagreements(recorded, junit)
    text = format_totals(recorded)
    if problems:
        text += "\njunit disagreement:\n" + "\n".join(problems)
    _write_summary(summary_path, text)
    failed = _int(recorded.get("failed"))
    errors = _int(recorded.get("errors"))
    collected = _int(recorded.get("collected"))
    if problems or failed or errors or collected <= 0 or exit_code not in (None, 0):
        return 1
    return 0


def write_session_totals(session: object, exitstatus: object, path: Path | None = None) -> Path:
    """Write the terminal reporter's counts. Called from pytest_sessionfinish."""
    config = session.config  # type: ignore[attr-defined]
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter is not None else {}
    runtime = 0.0
    start = getattr(reporter, "_session_start", None)
    elapsed = getattr(start, "elapsed", None)
    if callable(elapsed):
        runtime = float(elapsed().seconds)
    destination = path or Path(os.environ.get("PYTEST_TOTALS_PATH", "pytest-totals.json"))
    payload = totals_from_stats(
        stats,
        collected=int(getattr(session, "testscollected", 0) or 0),
        exit_code=int(exitstatus),
        runtime=runtime,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def pytest_sessionfinish(session: object, exitstatus: object) -> None:
    """Pytest hook. A failed suite still writes the file before the process exits."""
    if os.environ.get("PYTEST_TOTALS_DISABLE") == "1":
        return
    write_session_totals(session, exitstatus)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--exit-file", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = args.summary
    if summary is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    try:
        return publish(args.stats, args.junit, args.exit_file, summary)
    except (ET.ParseError, ValueError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"pytest totals\nrecord: unreadable\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
