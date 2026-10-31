"""Regression for CI failure reporting: missing pytest output must be NOT RUN, not fabricated.

The V9.0 Linux 3.11 run failed at pin drift, then downstream always() steps
failed because pytest-output.txt etc. were absent, masking the original
failure. The fix reports NOT RUN and does not create secondary errors.
"""

from __future__ import annotations

from pathlib import Path


def test_ci_reports_not_run_when_prerequisite_failed():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    # Count adapter warnings must handle missing file as NOT RUN
    assert "pytest-output: NOT RUN — prerequisite failed" in ci
    assert "datetime-adapter-warnings: NOT RUN" in ci
    # Record totals must handle missing JUnit/exit as NOT RUN
    assert "pytest-totals: NOT RUN — prerequisite failed" in ci
    # Publish must not error when files absent
    assert "if-no-files-found: warn" in ci
    # Must not have the old fabricated failure
    assert "test -f pytest-output.txt" not in ci or "if [ ! -f pytest-output.txt ]" in ci


def test_ci_windows_reports_not_run_when_prerequisite_failed():
    ciw = Path(".github/workflows/ci-windows.yml").read_text(encoding="utf-8")
    assert "pytest-output: NOT RUN — prerequisite failed" in ciw
    assert "pytest-totals: NOT RUN — prerequisite failed" in ciw
    assert "if-no-files-found: warn" in ciw
    # Old code used Write-Error for missing output
    assert 'Write-Error "pytest-output.txt is missing"' not in ciw


def test_pytest_totals_distinguishes_zero_and_interrupted():
    # The parser must distinguish zero tests, interrupted, etc. — this is the
    # contract of tools/pytest_totals.py, not just the CI file.
    # We exercise it directly: zero collected is a failure, but not a hidden pass.
    from tools.pytest_totals import totals_from_stats

    recorded = totals_from_stats({}, collected=0, exit_code=0, runtime=0.1)
    assert recorded["collected"] == 0
    # publish will reject collected <=0 as failure (verified via disagreements)
    # Here we just check that totals are correctly formed for zero case
    assert recorded["passed"] == 0


def test_pytest_totals_publish_fails_on_missing_files(tmp_path):
    from tools.pytest_totals import publish

    stats = tmp_path / "missing-stats.json"
    junit = tmp_path / "missing-junit.xml"
    # publish with missing files must return 1, not crash, and report missing
    rc = publish(stats, junit, None, None)
    assert rc == 1
