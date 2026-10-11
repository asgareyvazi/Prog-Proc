"""CI test totals stay numeric when a suite fails midway."""

from __future__ import annotations

import json
from pathlib import Path

from tools.pytest_totals import (
    disagreements,
    format_totals,
    publish,
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
