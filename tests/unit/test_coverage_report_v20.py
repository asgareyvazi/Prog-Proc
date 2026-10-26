"""V8.20 coverage semantics: comparability, empty denominator, missing XML, platform."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "coverage_report_v20", ROOT / "tools" / "coverage_report.py"
)
assert SPEC is not None and SPEC.loader is not None
cr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cr
SPEC.loader.exec_module(cr)


def _view(
    scope="full-suite",
    selection="pytest (full suite)",
    deps=None,
    runtime="CPython 3.11.2 on Linux 6.8",
    schema=3,
):
    if deps is None:
        deps = [{"name": "ui", "present": True}, {"name": "mineru", "present": False}]
    return {
        "measurement_scope": scope,
        "test_selection": selection,
        "test_selection_fingerprint": "abc123",
        "schema_version": schema,
        "optional_dependencies": deps,
        "runtime": runtime,
        "platform": "Linux-6.8-x86_64",
        "python_version": "3.11.2",
        "coverage_version": "7.0",
        "groups": {"core": {"line_percent": 80.0}, "qt/ui": {"line_percent": 70.0}},
        "overall": {"line_percent": 75.0},
    }


def test_comparable_same_scope_selection_and_env():
    v = _view()
    b = _view()
    ok, reason = cr._baseline_comparable(v, b)
    assert ok, reason
    assert cr.compare(v, b)[0].startswith("core:")


def test_not_comparable_different_selection():
    v = _view(selection="tests/ui + tests/unit/test_qt_*")
    b = _view(selection="pytest (full suite)")
    ok, reason = cr._baseline_comparable(v, b)
    assert not ok
    assert "test_selection" in reason
    lines = cr.compare(v, b)
    assert any("not comparable" in line for line in lines)
    assert not any(
        "->" in line and "points" in line for line in lines if "not comparable" not in line
    )


def test_not_comparable_qt_absent_vs_present():
    v = _view(scope="ui-test-selection", deps=[{"name": "ui", "present": True}])
    b = _view(scope="ui-test-selection", deps=[{"name": "ui", "present": False}])
    ok, _ = cr._baseline_comparable(v, b)
    assert not ok

    # reverse also not comparable
    ok2, _ = cr._baseline_comparable(b, v)
    assert not ok2


def test_not_comparable_unknown_dependency():
    v = _view(scope="ui-test-selection", deps=[{"name": "ui", "present": True}])
    b = _view(scope="ui-test-selection", deps=[{"name": "ui", "present": None}])
    ok, _ = cr._baseline_comparable(v, b)
    assert not ok
    # also missing entirely
    v2 = _view(scope="ui-test-selection", deps=[])
    b2 = _view(scope="ui-test-selection", deps=[{"name": "ui", "present": True}])
    ok2, _ = cr._baseline_comparable(v2, b2)
    assert not ok2


def test_not_comparable_different_platform_or_python():
    v = _view(scope="ui-test-selection", runtime="CPython 3.11.2 on Linux 6.8")
    b = _view(scope="ui-test-selection", runtime="CPython 3.14.0 on Linux 6.8")
    ok, reason = cr._baseline_comparable(v, b)
    assert not ok
    assert "Python" in reason

    v2 = _view(scope="ui-test-selection", runtime="CPython 3.11.2 on Linux 6.8")
    b2 = _view(scope="ui-test-selection", runtime="CPython 3.11.2 on Windows 10")
    ok2, reason2 = cr._baseline_comparable(v2, b2)
    assert not ok2
    assert "platform" in reason2


def test_full_suite_vs_ui_not_comparable():
    v = _view(scope="ui-test-selection")
    b = _view(scope="full-suite")
    ok, _ = cr._baseline_comparable(v, b)
    assert not ok


def test_legacy_baseline_no_selection_not_comparable():
    v = _view(selection="tests/ui + ...", deps=[{"name": "ui", "present": True}])
    b = _view(selection="unknown", deps=[{"name": "ui", "present": True}])
    # legacy has unknown selection
    b["test_selection"] = "unknown"
    ok, _ = cr._baseline_comparable(v, b)
    assert not ok


def test_invalid_baseline_json_or_schema_mismatch():
    v = _view(schema=3)
    b = _view(schema=2)
    ok, reason = cr._baseline_comparable(v, b)
    assert not ok
    assert "schema" in reason

    # missing baseline
    ok2, _ = cr._baseline_comparable(v, None)
    assert not ok2


def test_comparable_improvement_delta():
    v = _view()
    b = _view()
    v["groups"]["core"]["line_percent"] = 90.0
    b["groups"]["core"]["line_percent"] = 80.0
    v["overall"]["line_percent"] = 85.0
    b["overall"]["line_percent"] = 80.0
    lines = cr.compare(v, b)
    assert any("core: 80.00% -> 90.00% (+10.00 points)" in line for line in lines)
    assert any("overall:" in line for line in lines)


def test_comparable_negative_delta():
    v = _view()
    b = _view()
    v["groups"]["core"]["line_percent"] = 70.0
    b["groups"]["core"]["line_percent"] = 80.0
    lines = cr.compare(v, b)
    assert any("70.00%" in line and "-10.00" in line for line in lines)


def test_mismatched_selection_no_arrow():
    v = _view(selection="A")
    b = _view(selection="B")
    lines = cr.compare(v, b)
    # should not have arrow delta for groups
    assert not any("->" in line and "points" in line for line in lines if "core:" in line)


def test_empty_denominator_json_is_null_not_100():
    empty = cr.GroupCoverage(name="empty")
    assert empty.line_percent is None
    assert empty.branch_percent is None
    d = empty.as_dict()
    assert d["line_percent"] is None
    assert d["branch_percent"] is None
    assert d["present"] is False
    assert d["measurement_state"] == "not_measured"
    # File with 0 branches but statements
    gc = cr.GroupCoverage(name="x")
    gc.files = [
        cr.FileCoverage(
            path="a", statements=10, covered_statements=5, branches=0, covered_branches=0
        )
    ]
    assert gc.line_percent == 50.0
    assert gc.branch_percent is None
    assert gc.as_dict()["branch_percent"] is None
    assert gc.as_dict()["line_percent"] == 50.0
    # Markdown must show not measured, not 100%
    view = {
        "source": "coverage.xml",
        "xml_path": "coverage.xml",
        "measurement_scope": "full-suite",
        "test_selection": "pytest",
        "test_selection_fingerprint": "abc",
        "commit": "abc123",
        "runtime": "CPython 3.11 on Linux",
        "platform": "Linux",
        "python_version": "3.11",
        "coverage_version": "7.0",
        "schema_version": 3,
        "optional_dependencies": [],
        "overall": {
            "covered_statements": 0,
            "statements": 0,
            "covered_branches": 0,
            "branches": 0,
            "line_percent": None,
            "branch_percent": None,
        },
        "groups": {"empty": empty.as_dict()},
    }
    md = cr.render_markdown(view, None)
    assert "— (not measured)" in md
    assert "100.0%" not in md or "not measured" in md


def test_measured_zero_is_true_zero():
    gc = cr.GroupCoverage(name="y")
    gc.files = [
        cr.FileCoverage(
            path="b", statements=10, covered_statements=0, branches=4, covered_branches=0
        )
    ]
    assert gc.line_percent == 0.0
    assert gc.branch_percent == 0.0
    assert gc.present is True
    assert gc.measurement_state == "measured"
    view = {
        "source": "coverage.xml",
        "xml_path": "coverage.xml",
        "measurement_scope": "full-suite",
        "test_selection": "pytest",
        "test_selection_fingerprint": "abc",
        "commit": "abc",
        "runtime": "CPython 3.11 on Linux",
        "platform": "Linux",
        "python_version": "3.11",
        "coverage_version": "7.0",
        "schema_version": 3,
        "optional_dependencies": [],
        "overall": gc.as_dict(),
        "groups": {"y": gc.as_dict()},
    }
    md = cr.render_markdown(view, None)
    assert "0.0%" in md
    # JSON should have 0.0, not None
    assert view["groups"]["y"]["line_percent"] == 0.0


def test_missing_xml_strict_fails(tmp_path):
    missing = tmp_path / "missing.xml"
    assert cr.main(["--xml", str(missing)]) == 2
    assert cr.main(["--xml", str(missing), "--allow-missing"]) == 0
    # Also test that non-existent baseline doesn't affect
    tmp_path.joinpath("baseline.json").write_text("{}", encoding="utf-8")
