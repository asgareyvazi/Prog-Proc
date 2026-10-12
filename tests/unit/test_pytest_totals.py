"""CI test totals stay numeric when a suite fails midway."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools.pytest_totals import (
    ExitRecordError,
    disagreements,
    format_totals,
    publish,
    read_exit_file,
    skip_is_expected,
    totals_from_junit,
    totals_from_stats,
)

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="1" failures="2" skipped="3" tests="8" time="1.500">
  <testcase classname="a" name="ok"/>
  <testcase classname="a" name="bad"><failure message="nope"/></testcase>
  <testcase classname="a" name="bad2"><failure message="nope"/></testcase>
  <testcase classname="a" name="boom"><error message="setup"/></testcase>
  <testcase classname="a" name="skip"><skipped type="pytest.skip" message="later"/></testcase>
  <testcase classname="a" name="xfail"><skipped type="pytest.xfail" message="known"/></testcase>
  <testcase classname="a" name="xpass">
    <skipped message="xfail-marked test passes unexpectedly"/>
  </testcase>
</testsuite>
"""


def _stats_file(path: Path, **overrides: int | float) -> None:
    payload = {
        "collected": 8,
        "passed": 2,
        "failed": 2,
        "errors": 1,
        "skipped": 1,
        "xfailed": 1,
        "xpassed": 1,
        "exit": 1,
        "runtime": 1.5,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_junit_totals_split_skips_from_xfail_and_xpass(tmp_path: Path) -> None:
    path = tmp_path / "pytest-junit.xml"
    path.write_text(_JUNIT, encoding="utf-8")
    totals = totals_from_junit(path, exit_code=1)
    assert totals["collected"] == 8
    assert totals["failed"] == 2
    assert totals["errors"] == 1
    assert totals["skipped"] == 1
    assert totals["xfailed"] == 1
    assert totals["xpassed"] == 1
    assert "collected: 8" in format_totals(totals)


def test_session_stats_count_xpass_that_junit_folds_into_passed() -> None:
    totals = totals_from_stats(
        {"passed": [object()], "xpassed": [object()], "skipped": [object()]},
        collected=3,
        exit_code=0,
        runtime=0.2,
    )
    junit = {
        "collected": 3,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "xfailed": 0,
        "xpassed": 0,
    }
    assert totals["xpassed"] == 1
    assert disagreements(totals, junit) == []


def test_a_missing_record_fails_even_if_the_other_file_exists(tmp_path: Path) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_JUNIT, encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = publish(tmp_path / "missing.json", junit, None, summary)
    assert code == 1
    text = summary.read_text(encoding="utf-8")
    assert "stats: missing" in text
    assert "junit: present" in text


def _clean_junit(tests: int = 2, failed: int = 0, skipped: int = 0) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="pytest" errors="0" failures="{failed}" skipped="{skipped}" '
        f'tests="{tests}" time="0.100">'
        '<testcase classname="tests.unit.demo" name="ok"/>'
        "</testsuite>"
    )


def test_a_supplied_missing_exit_file_is_not_replaced_by_session_exit(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    stats.write_text(
        json.dumps(
            {
                "collected": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0.1,
            }
        ),
        encoding="utf-8",
    )
    junit.write_text(_clean_junit(tests=1), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = publish(stats, junit, tmp_path / "missing-exit.txt", summary)
    assert code == 1
    assert "exit file missing" in summary.read_text(encoding="utf-8")


def test_shell_and_session_exits_are_compared(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats, collected=2, passed=2, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0, exit=1
    )
    junit.write_text(_clean_junit(), encoding="utf-8")
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert publish(stats, junit, exit_file, summary) == 1
    text = summary.read_text(encoding="utf-8")
    assert "shell 0 != session 1" in text
    exit_file.write_text("\n", encoding="utf-8")
    try:
        read_exit_file(exit_file)
    except ExitRecordError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty exit file must fail")
    exit_file.write_text("nope\n", encoding="utf-8")
    try:
        read_exit_file(exit_file)
    except ExitRecordError as exc:
        assert "integer" in str(exc)
    else:
        raise AssertionError("malformed exit file must fail")


def test_unexpected_xpass_and_skip_fail_an_otherwise_green_record(tmp_path: Path) -> None:
    assert skip_is_expected(
        "tests.ui.test_workbench",
        "test_real",
        "QtWidgets unavailable for UI tests: libGL",
    )
    assert not skip_is_expected("tests.integration.test_x", "test_y", "symlinks")
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats,
        collected=1,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=1,
        exit=0,
    )
    junit.write_text(_clean_junit(tests=1), encoding="utf-8")
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 1


def test_a_known_ui_skip_can_still_publish_a_green_record(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats,
        collected=2,
        passed=1,
        failed=0,
        errors=0,
        skipped=1,
        xfailed=0,
        xpassed=0,
        exit=0,
    )
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <testsuite name="pytest" errors="0" failures="0" skipped="1" tests="2" time="0.1">
          <testcase classname="tests.unit.demo" name="ok"/>
          <testcase classname="tests.ui.test_workbench" name="test_window">
            <skipped type="pytest.skip" message="QtWidgets unavailable for UI tests: libGL"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 0


def test_real_pytest_accounts_for_pass_fail_skip_xfail_and_xpass(tmp_path: Path) -> None:
    suite = tmp_path / "test_outcomes.py"
    suite.write_text(
        "import pytest\n"
        "def test_ok():\n"
        "    assert True\n"
        "def test_bad():\n"
        "    assert False\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_skip():\n"
        "    pass\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_xfail():\n"
        "    assert False\n"
        "@pytest.mark.xfail(reason='surprise')\n"
        "def test_xpass():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    totals = tmp_path / "totals.json"
    junit = tmp_path / "junit.xml"
    env = os.environ.copy()
    env["PYTEST_TOTALS_PATH"] = str(totals)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-p",
            "tools.pytest_totals",
            "--noconftest",
            "-o",
            "addopts=",
            "--junitxml",
            str(junit),
            "-q",
            "--tb=no",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 1
    payload = json.loads(totals.read_text(encoding="utf-8"))
    assert payload["collected"] == 5
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 1
    assert payload["xfailed"] == 1
    assert payload["xpassed"] == 1
    assert (
        payload["passed"]
        + payload["failed"]
        + payload["errors"]
        + payload["skipped"]
        + payload["xfailed"]
        + payload["xpassed"]
        == payload["collected"]
    )
    parsed = totals_from_junit(junit, exit_code=1)
    assert disagreements(payload, parsed) == []


def test_a_disagreement_or_a_failed_suite_fails_the_record(tmp_path: Path) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_JUNIT, encoding="utf-8")
    stats = tmp_path / "pytest-totals.json"
    _stats_file(stats, failed=0)
    assert publish(stats, junit, None, None) == 1
    _stats_file(stats)
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("1\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 1
