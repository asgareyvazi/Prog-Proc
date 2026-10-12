#!/usr/bin/env python3
"""Publish explicit pytest totals.

The CI log uses one quiet level, which still prints a summary when the process
finishes. This record is what remains when that line is missing or the suite
failed. JUnit does not mark an xpass, so the session stats are the authority
and JUnit is the cross-check. When ``--exit-file`` is supplied, that file is a
required independent record: a missing, empty, or malformed file fails the
publisher, and its integer is compared with the session exit. A green JSON/JUnit
pair must not conceal it.

An unexpected pass fails the record. A skip is expected only for a headless UI
test whose reason says Qt could not load. Every other skip fails the record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_XFAIL_TYPE = "pytest.xfail"
_XPASS_MESSAGE = "xfail-marked test passes unexpectedly"
_EXIT_LINE = re.compile(r"-?\d+")
_HEADLESS_SKIP_REASON = "QtWidgets unavailable"
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


class ExitRecordError(ValueError):
    """A supplied shell-exit file is missing or not one integer."""


class RecordError(ValueError):
    """A stats or JUnit record has the wrong shape.

    The message is a category. It must not include the file body.
    """


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _suites(root: ET.Element) -> list[ET.Element]:
    if root.tag == "testsuite":
        return [root]
    return list(root.findall("testsuite"))


def _nonnegative_int(raw: str | None, label: str) -> int:
    if raw is None or raw == "":
        return 0
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise RecordError(f"{label} is not a non-negative integer")
    return int(raw)


def _nonnegative_float(raw: str | None, label: str) -> float:
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise RecordError(f"{label} is not a finite non-negative number") from exc
    if value != value or value < 0 or value == float("inf"):
        raise RecordError(f"{label} is not a finite non-negative number")
    return value


def totals_from_junit(path: Path, exit_code: int | None = None) -> dict[str, int | float | None]:
    """Count outcomes. xfail is split out of the skipped bucket.

    An unexpected pass is a normal ``testcase`` in JUnit, so ``xpassed`` here
    stays 0. ``totals_from_stats`` is the authority for that count. A wrong
    root or a non-numeric count fails closed without the document body.
    """
    # The file is pytest's own JUnit artifact, not an external document.
    try:
        root = ET.parse(path).getroot()  # noqa: S314
    except ET.ParseError as exc:
        raise RecordError("junit is not xml") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise RecordError("junit root is not a testsuite")
    suites = _suites(root)
    if not suites:
        raise RecordError("junit has no testsuite")
    collected = failures = errors = skipped_attr = 0
    runtime = 0.0
    xfailed = xpassed = 0
    for suite in suites:
        collected += _nonnegative_int(suite.attrib.get("tests"), "junit tests")
        failures += _nonnegative_int(suite.attrib.get("failures"), "junit failures")
        errors += _nonnegative_int(suite.attrib.get("errors"), "junit errors")
        skipped_attr += _nonnegative_int(suite.attrib.get("skipped"), "junit skipped")
        runtime += _nonnegative_float(suite.attrib.get("time"), "junit time")
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
    accounted = (
        _int(recorded.get("passed"))
        + _int(recorded.get("failed"))
        + _int(recorded.get("errors"))
        + _int(recorded.get("skipped"))
        + _int(recorded.get("xfailed"))
        + _int(recorded.get("xpassed"))
    )
    if accounted != _int(recorded.get("collected")):
        problems.append(f"accounted {accounted} != collected {recorded.get('collected')}")
    return problems


def skip_is_expected(classname: str, name: str, message: str) -> bool:
    """Headless CI may skip UI tests only because Qt itself cannot load."""
    identity = f"{classname} {name}"
    ui = "tests.ui." in classname or classname.startswith("tests.ui") or "tests/ui/" in identity
    return ui and _HEADLESS_SKIP_REASON in (message or "")


def unexpected_skips(path: Path) -> list[str]:
    """JUnit skips that are not the known headless Qt skip."""
    root = ET.parse(path).getroot()  # noqa: S314
    found: list[str] = []
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        if skipped is None:
            continue
        kind = skipped.attrib.get("type") or ""
        if kind == _XFAIL_TYPE:
            continue
        message = skipped.attrib.get("message") or ""
        if message.startswith(_XPASS_MESSAGE):
            continue
        classname = case.attrib.get("classname") or ""
        name = case.attrib.get("name") or ""
        if skip_is_expected(classname, name, message):
            continue
        reason = " ".join(message.split())[:120] or "no reason"
        found.append(f"{classname}::{name}: {reason}")
    return found


def format_totals(totals: dict[str, int | float | None]) -> str:
    lines = ["pytest totals"]
    for key in _KEYS:
        lines.append(f"{key}: {totals.get(key)}")
    return "\n".join(lines)


def read_exit_file(path: Path) -> int:
    """Read one integer. A supplied path that is missing or malformed is an error.

    Omitting the path is the standalone contract. Supplying it, as CI does, means
    the file is required and is not replaced by the session-recorded exit.
    """
    if not path.is_file():
        raise ExitRecordError("exit file missing")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExitRecordError("exit file unreadable") from exc
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise ExitRecordError("exit file empty")
    if len(lines) != 1 or _EXIT_LINE.fullmatch(lines[0]) is None:
        raise ExitRecordError("exit file is not one integer")
    return int(lines[0])


_NOTICE_PREFIXES = (
    "collected:",
    "passed:",
    "failed:",
    "errors:",
    "skipped:",
    "xfailed:",
    "xpassed:",
    "exit:",
    "shell_exit:",
    "session_exit:",
    "disagreement:",
    "runtime:",
    "stats:",
    "junit:",
)


def notice_lines(text: str) -> list[str]:
    """Short annotation lines. They must not include file contents or secrets."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_NOTICE_PREFIXES):
            lines.append(stripped[:200])
    return lines


def _write_summary(summary_path: Path | None, text: str) -> None:
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in notice_lines(text):
            print(f"::notice::{line}")
    if summary_path is None:
        return
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write("### Pytest totals\n\n```\n" + text + "\n```\n")


def _load_stats(path: Path) -> dict[str, int | float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordError("stats is not json") from exc
    if not isinstance(payload, dict):
        raise RecordError("stats root is not an object")
    totals: dict[str, int | float] = {}
    for key in _KEYS:
        if key not in payload:
            raise RecordError(f"stats missing {key}")
        value = payload[key]
        if key == "runtime":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RecordError("stats runtime is not a finite non-negative number")
            if value != value or value < 0 or value in (float("inf"), float("-inf")):
                raise RecordError("stats runtime is not a finite non-negative number")
            totals[key] = float(value)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecordError(f"stats {key} is not a non-negative integer")
        totals[key] = value
    return totals


def publish(
    stats_path: Path,
    junit_path: Path,
    exit_file: Path | None,
    summary_path: Path | None,
) -> int:
    """Print totals and fail unless the required records exist, agree, and passed.

    ``exit_file is None`` is the standalone library contract. A path, including a
    path that does not exist, is a required shell record.
    """
    shell_exit: int | None = None
    exit_problem = ""
    if exit_file is not None:
        try:
            shell_exit = read_exit_file(exit_file)
        except ExitRecordError as exc:
            exit_problem = str(exc)
    if not stats_path.is_file() or not junit_path.is_file() or exit_problem:
        text = "\n".join(
            [
                "pytest totals",
                f"stats: {'present' if stats_path.is_file() else 'missing'}",
                f"junit: {'present' if junit_path.is_file() else 'missing'}",
                f"shell_exit: {exit_problem or shell_exit if exit_file is not None else 'not-requested'}",
            ]
        )
        _write_summary(summary_path, text)
        return 1
    try:
        recorded = _load_stats(stats_path)
        junit = totals_from_junit(junit_path, shell_exit)
        skips = unexpected_skips(junit_path)
    except RecordError as exc:
        text = "pytest totals\nrecord: unreadable\n" + str(exc)
        _write_summary(summary_path, text)
        return 1
    except ET.ParseError:
        text = "pytest totals\nrecord: unreadable\njunit is not xml"
        _write_summary(summary_path, text)
        return 1
    session_exit = recorded["exit"]
    problems = disagreements(recorded, junit)
    if shell_exit is not None and _int(session_exit) != shell_exit:
        problems.append(f"exit: shell {shell_exit} != session {session_exit}")
    if _int(recorded.get("xpassed")):
        problems.append(f"xpassed: {recorded.get('xpassed')} unexpected pass is not a release pass")
    if skips:
        problems.append("unexpected skip:\n" + "\n".join(skips))
    recorded = dict(recorded)
    recorded["shell_exit"] = shell_exit if shell_exit is not None else "not-requested"
    recorded["session_exit"] = session_exit
    recorded["exit"] = shell_exit if shell_exit is not None else session_exit
    text = format_totals(recorded)
    text += f"\nshell_exit: {recorded['shell_exit']}\nsession_exit: {session_exit}"
    if problems:
        text += "\ndisagreement:\n" + "\n".join(problems)
    _write_summary(summary_path, text)
    failed = _int(recorded.get("failed"))
    errors = _int(recorded.get("errors"))
    collected = _int(recorded.get("collected"))
    exit_bad = shell_exit not in (None, 0) or (shell_exit is None and _int(session_exit) != 0)
    if problems or failed or errors or collected <= 0 or exit_bad:
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
    except (
        ET.ParseError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        RecordError,
    ):
        # TypeError is a backstop. Known bad shapes are rejected by RecordError
        # before this handler, and this line does not print the exception.
        print("pytest totals\nrecord: unreadable")
        return 1


if __name__ == "__main__":
    sys.exit(main())
